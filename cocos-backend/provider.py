# provider.py

from abc import ABC, abstractmethod
from req_types import ServeRequest, ServeResponse, ClusterRequest, ClusterResponse, ServiceStatusResponse, ServiceEndpointResponse

class Provider(ABC):
    """
    Abstract base class for model serving and orchestration providers (e.g., SLURM, Kubernetes).
    """

    @abstractmethod
    def launch_serving_task(self, request: ServeRequest) -> ServeResponse:
        """
        Launch a serving task (model server).
        """
        pass

    @abstractmethod
    def stop_serving_task(self, task_id: str) -> ClusterResponse:
        """
        Stop a running serving task.
        """
        pass

    @abstractmethod
    def get_serving_task_status(self, task_ids) -> list[ServiceStatusResponse]:
        """
        Check status of one or more serving tasks.
        """
        pass

    @abstractmethod
    def get_serving_task_endpoint(self, task_id: str) -> ServiceEndpointResponse:
        """
        Retrieve the endpoint (URL) of a deployed model.
        """
        pass

    @abstractmethod
    def launch_finetune_task(self, request: ClusterRequest) -> ClusterResponse:
        """
        Launch a fine-tuning task.
        """
        pass

    @abstractmethod
    def stop_finetune_task(self, task_id: str) -> ClusterResponse:
        """
        Stop a fine-tuning task.
        """
        pass

    @abstractmethod
    def get_finetune_task_status(self, job_id: str) -> ClusterResponse:
        """
        Check the status of a fine-tuning job.
        """
        pass

    @abstractmethod
    def launch_orchestrator_task(self, compound_id: str, node_services: dict, flow_config: dict, execution_config: dict) -> ServeResponse:
        """
        Launch an orchestrator for a modular compound model.
        """
        pass
