from req_types import ServeRequest, ServeResponse, ClusterRequest, ClusterResponse, ServiceStatusResponse, ServiceEndpointResponse, QueryRequest

import os
import sys
import paramiko
import time
import re
import subprocess
import random

from .provider import Provider

class SlurmProvider(Provider):
    REMOTE_USER = "ovidiu.ghibea"
    REMOTE_SERVER = "fep.grid.pub.ro"

    LAUNCH_SCRIPT = "./slurm_job.sh"
    CHECK_IP_SCRIPT = "./check_job_ip.sh"

    LOCAL_PORT = 8891
    REMOTE_PORT = 8891

    PORTS_MAP = {}

    def connect_ssh(self):
        """Establish SSH connection to the remote server"""
        print(f"Connecting to {self.REMOTE_USER}@{self.REMOTE_SERVER}...")
        
        # Create SSH client
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        try:
            # SSH Key Authentication only
            client.connect(self.REMOTE_SERVER, username=self.REMOTE_USER)
        except paramiko.AuthenticationException:
            pass
        
        return client
    
    def run_remote_command(self, client, command, print_output=True):
        print(f"Running command: {command}")
        stdin, stdout, stderr = client.exec_command(command)
        
        output = stdout.read().decode('utf-8').strip()
        error = stderr.read().decode('utf-8').strip()
        
        if print_output and output:
            print(output)
        
        if error:
            print(f"Error: {error}", file=sys.stderr)
        
        return output
    
    def submit_job(self, client, model_name, port):
        print(f"Submitting job for model {model_name} on port {port}...")
        # TODO: Run this on a separate thread.
        output = self.run_remote_command(client, self.LAUNCH_SCRIPT + f" {model_name} {port}", True)
        
        # Extract job ID using regex
        match = re.search(r'Job submitted with ID: (\d+)', output)
        if match:
            job_id = match.group(1)
            print(f"Job submitted successfully with ID: {job_id}")
            return job_id
        else:
            raise Exception("Failed to get job ID. Check if the launch script is working properly.")


    def get_ip_address(self, client, job_id):
        """Get the IP address for the job"""
        print(f"Retrieving IP address for job {job_id}...")
        command = f"{self.CHECK_IP_SCRIPT} {job_id}"
        ip_address = self.run_remote_command(client, command, False)
        
        if not ip_address:
            raise Exception("Failed to get IP address. Check if the check_ip script is working properly.")
        
        return ip_address

    def launch_serving_task(self, request: ServeRequest):
        client = self.connect_ssh()
        print("Connected to remote server")
        port = random.randint(22030, 27000)
        job_id = self.submit_job(client, request.model_name, port)
        self.PORTS_MAP[job_id] = port
        print("Job submitted on port " + str(port))
        print(f"Job ID: {job_id}")
        return ServeResponse(
            service_id=job_id,
            status="launching",
            message="Service deployment initiated"
        )
    
    def launch_finetune_task(self, request: ClusterRequest):
        pass

    def stop_serving_task(self, task_id: str):
        client = self.connect_ssh()
        command = f"scancel {task_id}"
        print("Running command: " + command)
        self.run_remote_command(client, command, False)

        return ClusterResponse(
            cluster_id=task_id,
            status="stopped",
            message="Cluster stopped successfully"
        )
    
    def stop_finetune_task(self, task_id: str):
        pass

    def get_finetune_task_status(self, job_id: str):
        pass
    
    def get_serving_task_status(self, task_ids):
        """
        Get status for multiple tasks. Can accept either a single task_id (str) 
        or a list of task_ids (list[str]). Always returns a list of ServiceStatusResponse.
        """
        # Handle both single task_id and list of task_ids for backwards compatibility
        if isinstance(task_ids, str):
            task_ids = [task_ids]
        
        status_responses = []
        
        for task_id in task_ids:
            try:
                print(f"Retrieving job status for {task_id}...")
                client = self.connect_ssh()
                command = f"squeue -j {task_id} -h -o %t"
                print("Running command: " + command)
                job_state = self.run_remote_command(client, command, False)
                client.close()

                if not job_state:
                    print(f"Task {task_id} not found in queue")
                    status_responses.append(ServiceStatusResponse(
                        service_id=task_id,
                        status="NOT_FOUND",
                        replicas=0,
                        message=f"Task {task_id} not found in queue"
                    ))
                    continue

                job_state = "READY" if job_state == "R" else job_state

                status_responses.append(ServiceStatusResponse(
                    service_id=task_id,
                    status=str(job_state),
                    replicas=0,
                    message="Status retrieved successfully"
                ))
                
            except Exception as e:
                print(f"Error getting status for task {task_id}: {e}")
                status_responses.append(ServiceStatusResponse(
                    service_id=task_id,
                    status="ERROR",
                    replicas=0,
                    message=f"Error retrieving status: {str(e)}"
                ))
        
        return status_responses
    
    def setup_ssh_tunnel(self, ip_address, port):
        """Set up a persistent reverse SSH tunnel using autossh in a container environment"""
        print(f"Setting up reverse SSH tunnel to {ip_address}:{port}...")
        
        # Create a unique identifier for this tunnel
        tunnel_id = f"{port}_{ip_address}_{port}"
        pid_file = f"/tmp/autossh_{tunnel_id}.pid"
        
        # Prepare the autossh command with proper flags
        tunnel_cmd = [
            "autossh",
            "-M", "0",  # Disable the monitoring port
            "-T",  # Disable pseudo-terminal allocation
            "-N",  # Do not execute a remote command
            "-4",  # Use IPv4 only
            "-f",  # Go to background
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-o", "ExitOnForwardFailure=yes",
            "-L", f"0.0.0.0:{port}:{ip_address}:{port}",
            f"{self.REMOTE_USER}@{self.REMOTE_SERVER}"
        ]
        
        try:
            # Clean up any previous instance with the same configuration
            cleanup_cmd = f"pkill -f 'autossh.*{port}:{ip_address}:{port}'"
            subprocess.run(cleanup_cmd, shell=True, stderr=subprocess.DEVNULL)
            
            # Execute the autossh command
            print("Executing command: " + " ".join(tunnel_cmd))
            
            # Run autossh in the background directly
            subprocess.run(tunnel_cmd, check=True)
            
            # Sleep to give the process time to establish
            time.sleep(3)
            
            # Check if the process is running by looking for the specific tunnel in ps
            check_cmd = f"ps aux | grep 'autossh.*{port}:{ip_address}:{port}' | grep -v grep"
            result = subprocess.run(check_cmd, shell=True, capture_output=True, text=True)
            
            if result.stdout.strip():
                # Extract PID from ps output
                pid = result.stdout.split()[1]
                print(f"SSH tunnel established successfully with PID {pid}. Listening on 0.0.0.0:{port}")
                
                # Save PID for future reference
                with open(pid_file, "w") as f:
                    f.write(pid)
                    
                return pid
            else:
                print(f"SSH tunnel failed to start or immediately terminated")
                return None
            
        except Exception as e:
            print(f"Error setting up SSH tunnel: {e}")
            return None

    def is_port_in_use(self, port):
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(('localhost', port))
                print("Port is free")
                return False
            except socket.error:
                print("Port is in use")
                return True

    def check_existing_tunnel(self, port):
        """Check if a tunnel is already running on the target port"""
        if self.is_port_in_use(port):
            print(f"A process is already listening on port {port}.")
            
            try:
                # On Linux
                cmd = f"ps aux | grep autossh | grep '{port}' | grep -v grep"
                process = subprocess.run(cmd, shell=True, capture_output=True, text=True)
                
                if process.stdout.strip():
                    print(f"Found existing autossh tunnel on port {port}:")
                    print(process.stdout.strip())
                    return True
            except Exception:
                pass
                
        return False

    import re

    def extract_ip(self, input_string):
        # Pattern to match IP addresses
        ip_pattern = r'\b(?:\d{1,3}\.){3}\d{1,3}\b'
        
        # Search for the pattern in the input string
        match = re.search(ip_pattern, input_string)
        
        # Return the matched IP address or None if not found
        return match.group(0) if match else None

    def get_serving_task_endpoint(self, job_id: str):
        
        client = self.connect_ssh()
        command = f"squeue -j {job_id} -h -o %t"
        job_state = self.run_remote_command(client, command, False)
        
        # If job not found, it might have failed
        if not job_state:
            raise Exception(f"Job {job_id} not found. It may have failed to start.")
        
        # If job is running, break out of the loop
        if job_state == "R":
            print(f"Job {job_id} is now running!")
            ip_address = self.get_ip_address(client, job_id)
            ip_address = self.extract_ip(ip_address)

            print(ip_address)
            port = self.PORTS_MAP[job_id]
            if not self.check_existing_tunnel(port):
                self.setup_ssh_tunnel(ip_address, port)
            else:
                print("Tunnel already exists, not creating a new one.")


            return ServiceEndpointResponse(
                service_id=job_id,
                # TODO: THis is the address of the current container
                endpoint=f'http://sky-api:{port}',
                status="success"
            )
        else:
            return ServiceEndpointResponse(
                service_id=job_id,
                # TODO: THis is the address of the current container
                endpoint=f'http://sky-api:{port}',
                status="fail"
            )

    
    def _generate_slurm_script(self, command, resources, job_name, output_file=None):
        pass
    
    def _submit_slurm_job(self, script_content):
        pass
    
    def _parse_slurm_job_status(self, status_output):
        pass

    def launch_orchestrator_task(self, compound_id: str, node_services: dict, flow_config: dict, execution_config: dict):
        """Launch an orchestrator service for compound system deployment on Slurm"""
        print(f"Launching SLURM orchestrator task for compound {compound_id}")
        
        try:
            client = self.connect_ssh()
            print("Connected to remote server for orchestrator deployment")
            
            orchestrator_service_id = f"{compound_id}-orchestrator"
            port = random.randint(22030, 27000)
            
            # Create configuration file content for the orchestrator
            orchestrator_config = {
                "compound_id": compound_id,
                "node_services": node_services,
                "flow_config": flow_config,
                "execution_config": execution_config
            }
            
            import json
            config_content = json.dumps(orchestrator_config, indent=2)
            
            # Create remote config file
            config_filename = f"orchestrator_config_{compound_id}.json"
            create_config_cmd = f"cat > {config_filename} << 'EOF'\n{config_content}\nEOF"
            self.run_remote_command(client, create_config_cmd, False)
            
            # Create orchestrator launch script
            orchestrator_script = f"""#!/bin/bash
#SBATCH --job-name=orchestrator-{compound_id}
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --mem=4G
#SBATCH --time=24:00:00
#SBATCH --output=orchestrator_{compound_id}_%j.out
#SBATCH --error=orchestrator_{compound_id}_%j.err

# Set up environment
export ORCHESTRATOR_CONFIG={config_filename}
export ORCHESTRATOR_PORT={port}
export COMPOUND_ID={compound_id}

# Install dependencies if needed
pip install --user fastapi uvicorn requests

# Start orchestrator service
echo "Starting orchestrator for compound {compound_id} on port {port}"
echo "Config file: {config_filename}"

# Create a simple orchestrator script
cat > orchestrator_{compound_id}.py << 'ORCH_EOF'
import json
import os
import uvicorn
from fastapi import FastAPI, HTTPException
import requests

app = FastAPI()

# Load configuration
config_file = os.environ.get('ORCHESTRATOR_CONFIG', 'config.json')
with open(config_file, 'r') as f:
    config = json.load(f)

compound_id = config['compound_id']
node_services = config['node_services']
flow_config = config['flow_config']
execution_config = config['execution_config']

@app.get("/health")
async def health_check():
    return {{"status": "healthy", "compound_id": compound_id}}

@app.get("/status")
async def get_status():
    return {{
        "compound_id": compound_id,
        "node_services": node_services,
        "flow_config": flow_config,
        "status": "running"
    }}

@app.post("/execute")
async def execute_compound(request_data: dict):
    # TODO: Implement compound execution logic based on flow_config
    # This would route requests through the different nodes based on the flow
    return {{"message": "Compound execution not yet implemented", "compound_id": compound_id}}

if __name__ == "__main__":
    port = int(os.environ.get('ORCHESTRATOR_PORT', 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
ORCH_EOF

# Run the orchestrator
python orchestrator_{compound_id}.py
"""
            
            # Write the script to a file
            script_filename = f"orchestrator_launch_{compound_id}.sh"
            create_script_cmd = f"cat > {script_filename} << 'EOF'\n{orchestrator_script}\nEOF"
            self.run_remote_command(client, create_script_cmd, False)
            
            # Make script executable
            chmod_cmd = f"chmod +x {script_filename}"
            self.run_remote_command(client, chmod_cmd, False)
            
            # Submit the orchestrator job
            submit_cmd = f"sbatch {script_filename}"
            output = self.run_remote_command(client, submit_cmd, True)
            
            # Extract job ID using regex
            match = re.search(r'Submitted batch job (\d+)', output)
            if match:
                job_id = match.group(1)
                orchestrator_job_id = f"orch-{job_id}"
                self.PORTS_MAP[orchestrator_job_id] = port
                print(f"Orchestrator job submitted successfully with ID: {job_id}")
                print(f"Orchestrator will run on port: {port}")
                
                client.close()
                
                return ServeResponse(
                    service_id=orchestrator_job_id,
                    status="launching",
                    message=f"Orchestrator deployment initiated for compound {compound_id}"
                )
            else:
                raise Exception("Failed to get orchestrator job ID. Check if the submission worked properly.")
                
        except Exception as e:
            print(f"Error launching orchestrator on Slurm: {e}")
            if 'client' in locals():
                client.close()
            raise Exception(f"Failed to launch orchestrator: {str(e)}")