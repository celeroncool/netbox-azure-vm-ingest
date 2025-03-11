import os
from azure.identity import ClientSecretCredential
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.network import NetworkManagementClient
from azure.mgmt.resource import ResourceManagementClient
from netboxlabs.diode.sdk import DiodeClient
from netboxlabs.diode.sdk.ingester import (
    Entity,
    VirtualMachine,
    VirtualDisk,
    VMInterface,
    IPAddress,
    Cluster,
    ClusterType,
    ClusterGroup,
)

# Azure Enterprise App authentication
tenant_id = os.environ.get("AZURE_TENANT_ID")
client_id = os.environ.get("AZURE_CLIENT_ID")
client_secret = os.environ.get("AZURE_CLIENT_SECRET")
subscription_id = os.environ.get("AZURE_SUBSCRIPTION_ID")

# Create credential object using Enterprise App (Service Principal)
credential = ClientSecretCredential(
    tenant_id=tenant_id,
    client_id=client_id,
    client_secret=client_secret
)

# Initialize Azure clients
compute_client = ComputeManagementClient(credential, subscription_id)
network_client = NetworkManagementClient(credential, subscription_id)
resource_client = ResourceManagementClient(credential, subscription_id)

# Diode authentication
diode_api_key = os.environ.get("DIODE_API_KEY")
diode_target = os.environ.get("DIODE_TARGET", "grpc://localhost:8080/diode")

# Initialize Diode client with authentication
diode_client = DiodeClient(
    target=diode_target,
    app_name="azure-vm-collector",
    app_version="0.1.0",
    api_key=diode_api_key
)

def get_vm_size_details(vm_size, location):
    """Get vCPU and memory details for a VM size"""
    try:
        size_info = compute_client.virtual_machine_sizes.list(location=location)
        for size in size_info:
            if size.name == vm_size:
                # Convert memory to integer GB (round up)
                memory_gb = int(size.memory_in_mb / 1024)
                return size.number_of_cores, memory_gb
    except Exception as e:
        print(f"Error getting VM size details: {str(e)}")
    return None, None

def get_vm_network_interfaces(vm, resource_group):
    """Get network interfaces and IP addresses for a VM"""
    interfaces = []
    ip_addresses = []

    for nic_ref in vm.network_profile.network_interfaces:
        nic_id = nic_ref.id
        nic_name = nic_id.split('/')[-1]

        # Get network interface details
        nic = network_client.network_interfaces.get(resource_group, nic_name)

        for ip_config in nic.ip_configurations:
            # Create VM interface using the VMInterface class
            vm_interface = VMInterface(
                name=nic_name,
                virtual_machine=vm.name,
                mac_address=nic.mac_address,
                enabled=True,
                description=f"Interface for {vm.name}"
            )

            # Create entity VM interface
            interfaces.append(Entity(vminterface=vm_interface))

            # Get IP address if available
            if ip_config.private_ip_address:
                # Create IP address without vm_interface field
                private_ip = IPAddress(
                    address=f"{ip_config.private_ip_address}/32",
                    status="active",
                    description=f"Private IP for {vm.name} on interface {nic_name}"
                )
                ip_addresses.append(Entity(ip_address=private_ip))

            # Get public IP if available
            if ip_config.public_ip_address:
                public_ip_id = ip_config.public_ip_address.id
                public_ip_name = public_ip_id.split('/')[-1]
                public_ip = network_client.public_ip_addresses.get(resource_group, public_ip_name)

                if public_ip.ip_address:
                    public_ip_entity = IPAddress(
                        address=f"{public_ip.ip_address}/32",
                        status="active",
                        description=f"Public IP for {vm.name} on interface {nic_name}"
                    )
                    ip_addresses.append(Entity(ip_address=public_ip_entity))

    return interfaces, ip_addresses

def get_vm_disks(vm, resource_group):
    """Get disk information for a VM"""
    disks = []
    total_disk_size = 0

    # OS disk
    os_disk_size = vm.storage_profile.os_disk.disk_size_gb or 0
    # Ensure disk size is an integer
    os_disk_size = int(os_disk_size)
    total_disk_size += os_disk_size

    os_disk = VirtualDisk(
        name=vm.storage_profile.os_disk.name,
        virtual_machine=vm.name,
        size=os_disk_size,
        description=f"OS Disk for {vm.name}"
    )
    disks.append(Entity(virtual_disk=os_disk))

    # Data disks
    for data_disk in vm.storage_profile.data_disks:
        disk_size = data_disk.disk_size_gb or 0
        # Ensure disk size is an integer
        disk_size = int(disk_size)
        total_disk_size += disk_size

        disk = VirtualDisk(
            name=data_disk.name,
            virtual_machine=vm.name,
            size=disk_size,
            description=f"Data Disk for {vm.name}"
        )
        disks.append(Entity(virtual_disk=disk))

    return disks, total_disk_size

def collect_azure_vms():
    """Collect Azure VM information and format for Diode"""
    entities = []
    regions = set()

    # Create a cluster group and type for Azure VMs
    cluster_group = ClusterGroup(
        name="Azure",
        description="Azure Cloud Resources"
    )
    entities.append(Entity(cluster_group=cluster_group))

    cluster_type = ClusterType(
        name="Azure Region",
        description="Azure Geographic Region"
    )
    entities.append(Entity(cluster_type=cluster_type))

    # First pass: collect all regions where VMs are located
    print("Collecting Azure regions...")
    for resource_group in resource_client.resource_groups.list():
        rg_name = resource_group.name

        for vm in compute_client.virtual_machines.list(rg_name):
            vm_details = compute_client.virtual_machines.get(rg_name, vm.name)
            regions.add(vm_details.location)

    print(f"Found VMs in {len(regions)} Azure regions: {', '.join(regions)}")

    # Create clusters for each region
    for region in regions:
        cluster = Cluster(
            name=f"Azure-{region}",
            type="Azure Region",
            group="Azure",
            description=f"Azure Region: {region}"
        )
        entities.append(Entity(cluster=cluster))

    # Second pass: process VMs and assign to region clusters
    print("Processing VMs...")
    vm_count = 0
    for resource_group in resource_client.resource_groups.list():
        rg_name = resource_group.name

        # Process VMs in the resource group
        for vm in compute_client.virtual_machines.list(rg_name):
            vm_count += 1
            # Get VM details
            vm_details = compute_client.virtual_machines.get(rg_name, vm.name, expand='instanceView')
            region = vm_details.location

            print(f"Processing VM: {vm.name} in {rg_name} (Region: {region})")

            # Determine OS details
            os_type = vm_details.storage_profile.os_disk.os_type
            os_name = "Unknown"
            os_version = "Unknown"

            # Try to get more detailed OS information from VM instance view
            if vm_details.instance_view and vm_details.instance_view.statuses:
                for status in vm_details.instance_view.statuses:
                    if status.code.startswith("OSName"):
                        os_name = status.display_status
                    elif status.code.startswith("OSVersion"):
                        os_version = status.display_status

            # Get VM size details (vCPU and memory)
            vcpus, memory = get_vm_size_details(vm_details.hardware_profile.vm_size, region)
            if vcpus is None:
                vcpus = 0
            if memory is None:
                memory = 0

            # Get disk information
            disks, total_disk_size = get_vm_disks(vm_details, rg_name)

            # Determine VM status
            vm_status = "offline"
            if vm_details.instance_view and vm_details.instance_view.statuses:
                for status in vm_details.instance_view.statuses:
                    if status.code == "PowerState/running":
                        vm_status = "active"
                        break

            # Get VM name data
            vm_name = vm_details.name
            vm_display_name = ""
            vm_hostname = ""

            if vm_details.tags and "DisplayName" in vm_details.tags:
                vm_display_name = vm_details.tags.get("DisplayName")
            else:
                vm_display_name = vm_name

            if hasattr(vm_details, 'os_profile') and vm_details.os_profile:
                vm_hostname = vm_details.os_profile.computer_name
            else:
                vm_hostname = vm_name

            # Create tags list including resource group
            tags = ["azure", os_type.lower(), vm_details.hardware_profile.vm_size, f"rg:{rg_name}"]

            # Add any existing Azure tags
            if vm_details.tags:
                for tag_key, tag_value in vm_details.tags.items():
                    # Convert tag value to string and limit length
                    tag_value_str = str(tag_value)[:50]  # Limit tag value length
                    tags.append(f"{tag_key}:{tag_value_str}")

            # Create VM entity with name data and assign to region cluster
            vm_entity = VirtualMachine(
                name=vm_name,
                cluster=f"Azure-{region}",  # Assign to region cluster
                vcpus=vcpus,
                memory=memory,
                disk=total_disk_size,
                platform=os_type,
                status=vm_status,
                comments=f"OS: {os_name} {os_version}\nHostname: {vm_hostname}\nDisplay Name: {vm_display_name}\nResource Group: {rg_name}",
                tags=tags
            )
            entities.append(Entity(virtual_machine=vm_entity))

            # Add disks to entities
            entities.extend(disks)

            # Get network interfaces and IP addresses
            interfaces, ip_addresses = get_vm_network_interfaces(vm_details, rg_name)
            entities.extend(interfaces)
            entities.extend(ip_addresses)

    print(f"Processed {vm_count} VMs across {len(regions)} regions")
    return entities

def main():
    try:
        print("Starting Azure VM data collection...")

        # Check if Diode API key is set
        if not diode_api_key:
            print("ERROR: DIODE_API_KEY environment variable is not set")
            print("Please set the DIODE_API_KEY environment variable with your Diode API key")
            return

        
        # Collect VM data
        entities = collect_azure_vms()

        print(f"Collected {len(entities)} entities from Azure")

        # Ingest data into Diode
        print("Ingesting data into NetBox via Diode...")
        response = diode_client.ingest(entities=entities)

        if response.errors:
            print(f"Errors during ingestion: {response.errors}")
        else:
            print(f"Successfully ingested {len(entities)} entities into NetBox via Diode")

    except Exception as e:
        import traceback
        print(f"Error collecting Azure VM data: {str(e)}")
        print(traceback.format_exc())
    finally:
        diode_client.close()

if __name__ == "__main__":
    main()
