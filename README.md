### Setup env parameters

```
 export AZURE_TENANT_ID="your-entra-tenant-id"
 export AZURE_CLIENT_ID="your-enterprise-app-client-id"
 export AZURE_CLIENT_SECRET="your-enterprise-app-client-secret"
 export AZURE_SUBSCRIPTION_ID="your-azure-subscription-id"
 export DIODE_TARGET="grpc://your-diode-server:8080/diode"
 export DIODE_CLIENT_ID="diode-ingest" # Diode v1 Client ID
 export DIODE_CLIENT_SECRET="diode-client-secret" # Diode v1 Client Secret
```

### Install required python packages

```
python3 -m venv ./venv
source ./venv/bin/activate
pip install azure-identity azure-mgmt-compute azure-mgmt-network azure-mgmt-resource netboxlabs-diode-sdk
```

### Run the script inside venv.

python ingest.py
