import logging
import os
import signal
import time
import datetime
import json
import requests
from requests_http_signature import HTTPSignatureAuth
from base64 import b64decode
import kubernetes
import schedule

class GracefulKiller:
    kill_now = False
    def __init__(self):
        signal.signal(signal.SIGINT, self.exit_gracefully)
        signal.signal(signal.SIGTERM, self.exit_gracefully)
    def exit_gracefully(self, *args):
        logging.info("Received termination signal.")
        self.kill_now = True

def get_basename():
    return "remotekube"

def invoke_remoteit_api(body):
    key_id = os.environ.get('R3_ACCESS_KEY_ID')
    key_secret_id = os.environ.get('R3_SECRET_ACCESS_KEY')
    host = 'api.remote.it'
    url_path = '/graphql/v1'
    content_type_header = 'application/json'
    content_length_header = str(len(body))
    headers = {
        'host': host,
        'path': url_path,
        'content-type': content_type_header,
        'content-length': content_length_header,
    }
    auth=HTTPSignatureAuth(algorithm="hmac-sha256",
                           key=b64decode(key_secret_id),
                           key_id=key_id,
                           headers=['(request-target)','host','date','content-type','content-length'])
    response = requests.post('https://' + host + url_path, auth=auth, headers=headers, data=body.encode('utf-8'))
    if response.status_code != 200:
        logging.error(response.status_code)
    return response.text

def get_remoteit_application_id(protocol):
    if protocol.upper() == "TCP":
        return 1
    if protocol.upper() == "UDP":
        return 32769
    return -1

def get_remoteit_devices():
    query = 'query { login { id email devices { items { id name state created lastReported services { protocol port name } } } } }'
    payload = {
        "query": query
    }
    return json.loads(invoke_remoteit_api(json.dumps(payload)))

def get_remoteit_inactive_devices():
    query = 'query getDevices($state: String, $name: String) { login { devices(state: $state, name: $name) { total hasMore items { id name state lastReported services { id name } } } } }'
    payload = {
        "query": query,
        "variables": {
            "state": "inactive",
            "name": get_basename()
        }
    }
    return json.loads(invoke_remoteit_api(json.dumps(payload)))

def get_registration_code(host, port, protocol):
    application_id = get_remoteit_application_id(protocol)
    account_id = get_remoteit_devices()["data"]["login"]["id"]
    query = 'query Registration($account: String, $name: String, $platform: Int, $services: [ServiceInput!]) {login {account(id: $account) {registrationCode(name: $name, platform: $platform, services: $services)registrationCommand(name: $name, platform: $platform, services: $services)}}}'
    payload={
        "query": query,
        "variables": {
            "account": account_id,
            "platform": 1219, # remoteit/remoteit-agent
            "services": [
                {
                    "name": get_basename(),
                    "host": host,
                    "application": application_id,
                    "port": port
                }
            ]
        }
    }
    response = invoke_remoteit_api(json.dumps(payload))
    registration_code = json.loads(response)["data"]["login"]["account"]["registrationCode"]
    return registration_code

def delete_remoteit_device(device_id):
    query = 'mutation query($deviceId: String!) {deleteDevice(deviceId: $deviceId) }'
    payload = {
        "query": query,
        "variables": {
            "deviceId": device_id
        }
    }
    response = invoke_remoteit_api(json.dumps(payload))
    logging.info("Deleted remoteit device: " + device_id)

# https://github.com/kubernetes-client/python/blob/master/examples/remote_cluster.py
# https://github.com/kubernetes-client/python/blob/master/kubernetes/README.md
def get_kube_api_client():
    config = kubernetes.client.Configuration()
    config.host = "https://" + os.getenv("KUBERNETES_SERVICE_HOST") + ":" + os.getenv("KUBERNETES_SERVICE_PORT")
    config.ssl_ca_cert = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    config.api_key['authorization'] = open("/var/run/secrets/kubernetes.io/serviceaccount/token").read()
    config.api_key_prefix['authorization'] = 'Bearer'
    return kubernetes.client.ApiClient(config)

def get_prefixed_name(name):
    return get_basename() + "-" + name

def get_k8s_label_name_managed():
    return get_basename() + "/managed"

def get_k8s_label_name_service():
    return  get_basename() + "/service"

def get_k8s_annotation_name_port():
    return get_basename() + "/service-port"

def get_k8s_annotation_name_protocol():
    return get_basename() + "/service-protocol"

def get_k8s_label_selector_managed():
    return get_k8s_label_name_managed() + "=true"

def get_k8s_service_attributes(item):
    mandatory_annotation_keys = [
        get_k8s_annotation_name_port(),
        get_k8s_annotation_name_protocol()
    ]
    service = {}
    service["name"] = item.metadata.name
    has_mandatory_annotations = True
    for a in mandatory_annotation_keys:
        try:
            service[a] = item.metadata.annotations[a]
        except (TypeError, KeyError):
            has_mandatory_annotations = False
    service["has_mandatory_annotations"] = has_mandatory_annotations
    return service

# Return only K8s Services that have mandatory annotations for remotekube
def get_annotated_k8s_services(kube_api_client, namespace):
    logging.info("Searching in namespace '" + namespace + "'")
    services = []
    try:
        ret = kubernetes.client.CoreV1Api(kube_api_client).list_namespaced_service(namespace)
    except kubernetes.client.rest.ApiException as e:
        logging.error("Exception when calling K8s API: %s\n" % e)
    for item in ret.items:
        service = get_k8s_service_attributes(item)
        if service["has_mandatory_annotations"] == True:
            services.append(service)
    return services

# Return metadata commonly used in remoteit K8s resources
def get_remoteit_k8s_metadata(namespace, service_name, resource_name):
    metadata = {
        "namespace": namespace,
        "name": resource_name,
        "labels": {
            get_k8s_label_name_managed(): "true",
            get_k8s_label_name_service(): service_name
        }
    }
    return metadata

def get_remoteit_k8s_configmaps(kube_api_client, namespace, field_selector):
    label_selector = get_k8s_label_selector_managed()
    configmaps = []
    try:
        core_v1 = kubernetes.client.CoreV1Api(kube_api_client)
    except kubernetes.client.rest.ApiException as e:
        logging.error("Exception when creating K8s API instance: %s\n" % e)
        return []
    try:
        configmaps = core_v1.list_namespaced_config_map(namespace=namespace, field_selector=field_selector, label_selector=label_selector)
    except kubernetes.client.rest.ApiException as e:
        logging.error("Exception when calling K8s API: %s\n" % e)
    return configmaps

def get_remoteit_k8s_deployments(kube_api_client, namespace, field_selector):
    label_selector = get_k8s_label_selector_managed()
    deployments = []
    try:
        apps_v1 = kubernetes.client.AppsV1Api(kube_api_client)
    except kubernetes.client.rest.ApiException as e:
        logging.error("Exception when creating K8s API instance: %s\n" % e)
        return
    try:
        deployments = apps_v1.list_namespaced_deployment(namespace=namespace, field_selector=field_selector, label_selector=label_selector)
    except kubernetes.client.rest.ApiException as e:
        logging.error("Exception when calling K8s API: %s\n" % e)
    return deployments

# Create or update K8s configmap attached to remoteit K8s deployment
def create_remoteit_k8s_configmap(kube_api_client, namespace, service_name, data):
    try:
        core_v1 = kubernetes.client.CoreV1Api(kube_api_client)
    except kubernetes.client.rest.ApiException as e:
        logging.error("Exception when creating K8s API instance: %s\n" % e)
        return
    configmap_name = get_prefixed_name(service_name)
    metadata = get_remoteit_k8s_metadata(namespace, service_name, configmap_name)
    body = kubernetes.client.V1ConfigMap(api_version="v1", kind="ConfigMap", metadata=metadata, data=data)
    field_selector=('metadata.name=' + configmap_name)
    configmaps = get_remoteit_k8s_configmaps(kube_api_client, namespace, field_selector)
    if len(configmaps.items) > 0:
        try:
            ret = core_v1.replace_namespaced_config_map(namespace=namespace, name=configmap_name, body=body)
            logging.info("Updated existing configmap '" + configmap_name + "' in namespace '" + namespace + "'")
        except kubernetes.client.rest.ApiException as e:
            logging.error("Exception when calling K8s API: %s\n" % e)
    else:
        try:
            ret = core_v1.create_namespaced_config_map(namespace=namespace, body=body)
            logging.info("Created a new configmap '" + configmap_name + "' in namespace '" + namespace + "'")
        except kubernetes.client.rest.ApiException as e:
            logging.error("Exception when calling K8s API: %s\n" % e)

# Create or update remoteit K8s deployment
def create_remoteit_k8s_deployment(kube_api_client, namespace, service_name):
    try:
         apps_v1 = kubernetes.client.AppsV1Api(kube_api_client)
    except kubernetes.client.rest.ApiException as e:
        logging.error("Exception when creating K8s API instance: %s\n" % e)
        return
    deployment_name = get_prefixed_name(service_name)
    configmap_name = deployment_name
    metadata = get_remoteit_k8s_metadata(namespace, service_name, deployment_name)
    spec = {
        "replicas": 1,
        "selector": {
            "matchLabels": { "app": deployment_name }
        },
        "template": {
            "metadata": {
                "labels": { "app": deployment_name }
            },
            "spec": {
                "containers": [
                    {
                        "name": deployment_name,
                        "image": "remoteit/remoteit-agent",
                        "imagePullPolicy": "IfNotPresent",
                        "envFrom": [ { "configMapRef": { "name": configmap_name } } ]
                    }
                ]
            }
        }
    }
    body = kubernetes.client.V1Deployment(api_version="apps/v1", kind="Deployment", metadata=metadata, spec=spec)
    field_selector=('metadata.name=' + deployment_name)
    deployments = get_remoteit_k8s_deployments(kube_api_client, namespace, field_selector)

    if len(deployments.items) > 0:
        try:
            ret = apps_v1.replace_namespaced_deployment(namespace=namespace, name=deployment_name, body=body)
            logging.info("Updated existing deployment '" + deployment_name + "' in namespace '" + namespace + "'")
        except ApiException as e:
            logging.error("Exception when calling K8s API: %s\n" % e)
    else:
        try:
            ret = apps_v1.create_namespaced_deployment(namespace=namespace, body=body)
            logging.info("Created a new deployment '" + deployment_name + "' in namespace '" + namespace + "'")
        except ApiException as e:
            logging.error("Exception when calling K8s API: %s\n" % e)

# Create remoteit K8s deployment and configmap
def create_remoteit_k8s_resources(kube_api_client, namespace, service):
    service_name = service["name"]
    logging.info("Creating/updating remoteit K8s resources for K8s service '" + service_name + "' in namespace '" + namespace + "'")
    code = get_registration_code(
               service["name"],
               int(service[get_k8s_annotation_name_port()]),
               service[get_k8s_annotation_name_protocol()]
           )
    create_remoteit_k8s_configmap(kube_api_client, namespace, service_name, {"R3_REGISTRATION_CODE": code})
    create_remoteit_k8s_deployment(kube_api_client, namespace, service_name)

# Create a remoteit K8s resource for each annotated K8s Service across all namespaces
def create_k8s_resources(kube_api_client):
    logging.info("Looking for K8s resources to create/update.")
    for namespace in os.getenv("REMOTEKUBE_NAMESPACES").split(","):
        services = get_annotated_k8s_services(kube_api_client, namespace)
        for service in services:
            create_remoteit_k8s_resources(kube_api_client, namespace, service)

def create_resources_job(kube_api_client):
    logging.info("Started creation job")
    create_k8s_resources(kube_api_client)

# Return whether a remotekube K8s resource is missing a corresponding K8s service
def is_orphaned_resource(services, resource, kind):
    logging.info("Checking whether K8s " + kind + " '" + resource.metadata.name + "' is orphaned")
    for service in services:
        if (get_prefixed_name(service["name"])) == resource.metadata.name:
            return False
    return True

def delete_remoteit_k8s_deployment(kube_api_client, namespace, deployment_name):
    try:
         apps_v1 = kubernetes.client.AppsV1Api(kube_api_client)
    except kubernetes.client.rest.ApiException as e:
        logging.error("Exception when creating K8s API instance: %s\n" % e)
        return
    try:
        ret = apps_v1.delete_namespaced_deployment(namespace=namespace, name=deployment_name)
        logging.info("Deleted deployment '" + deployment_name + "' in namespace '" + namespace + "'")
    except ApiException as e:
        logging.error("Exception when calling K8s API: %s\n" % e)

def delete_remoteit_k8s_configmap(kube_api_client, namespace, configmap_name):
    try:
        core_v1 = kubernetes.client.CoreV1Api(kube_api_client)
    except kubernetes.client.rest.ApiException as e:
        logging.error("Exception when creating K8s API instance: %s\n" % e)
        return
    try:
        ret = core_v1.delete_namespaced_config_map(namespace=namespace, name=configmap_name)
        logging.info("Deleted configmap '" + configmap_name + "' in namespace '" + namespace + "'")
    except kubernetes.client.rest.ApiException as e:
        logging.error("Exception when calling K8s API: %s\n" % e)

# Delete orphaned remotekube K8s resources in each namespace
def delete_remoteit_k8s_resources(kube_api_client):
    logging.info("Looking for deletable K8s resources.")
    for namespace in os.getenv("REMOTEKUBE_NAMESPACES").split(","):
        services = get_annotated_k8s_services(kube_api_client, namespace)
        deployments = get_remoteit_k8s_deployments(kube_api_client, namespace, None)
        for deployment in deployments.items:
            if is_orphaned_resource(services,deployment,"deployment"):
                delete_remoteit_k8s_deployment(kube_api_client, namespace, deployment.metadata.name)
        configmaps = get_remoteit_k8s_configmaps(kube_api_client, namespace, None)
        for configmap in configmaps.items:
            if is_orphaned_resource(services,configmap,"configmap"):
                delete_remoteit_k8s_configmap(kube_api_client, namespace, configmap.metadata.name)

def is_deletable_device(device):
    logging.info("Checking whether remoteit device '" + device["name"] + "' is inactive")
    if device["state"] != "inactive":
        return False
    if device["name"].startswith(get_basename() + "-") == False:
        return False
    dt_last = datetime.datetime.fromisoformat(device["lastReported"].replace('Z','+00:00'))
    dt_now  = datetime.datetime.now(datetime.timezone.utc)
    if (dt_now - dt_last).total_seconds() < 30:
        return False
    return True

# Delete inactive remoteit resources
def delete_remoteit_devices():
    logging.info("Looking for deletable remoteit devices.")
    devices = get_remoteit_inactive_devices()
    for device in devices["data"]["login"]["devices"]["items"]:
        if is_deletable_device(device):
            delete_remoteit_device(device["id"])

# Delete unnecessary remotekube-related resources in K8s cluster and Remote.it account
def delete_resources_job(kube_api_client):
    logging.info("Started cleanup job.")
    delete_remoteit_k8s_resources(kube_api_client)
    delete_remoteit_devices()

def main():
    logging.basicConfig(
        level=os.environ.get('LOGLEVEL', 'WARNING').upper(),
        format="%(asctime)s [%(levelname)s] %(name)s:%(lineno)s %(funcName)s: %(message)s"
    )
    kube_api_client = get_kube_api_client()
    schedule.every(10).seconds.do(create_resources_job, kube_api_client=kube_api_client)
    schedule.every(10).seconds.do(delete_resources_job, kube_api_client=kube_api_client)
    killer = GracefulKiller()
    while not killer.kill_now:
        schedule.run_pending()
        time.sleep(1)
    logging.info("Hasta la vista, baby!")

if __name__ == '__main__':
    main()
