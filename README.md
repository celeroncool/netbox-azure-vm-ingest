### Setup env parameters

```
export AZURE_TENANT_ID="your-tenant-id"
export AZURE_CLIENT_ID="your-enterprise-app-client-id"
export AZURE_CLIENT_SECRET="your-enterprise-app-client-secret"
export AZURE_SUBSCRIPTION_ID="your-subscription-id"
export DIODE_TARGET="grpc://your-diode-server:8080/diode"
```

### Install required python packages

```
pip install azure-identity azure-mgmt-compute azure-mgmt-network azure-mgmt-resource netboxlabs-diode-sdk
```

### Run the script from netbox.

python azure_vm_collector.py
