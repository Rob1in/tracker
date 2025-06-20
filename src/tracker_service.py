"""
This module provides a Viam Vision Service module
to perform face Re-Id.
"""

from asyncio import create_task
from typing import Any, ClassVar, Dict, List, Mapping, Optional, Sequence

from typing_extensions import Self
from viam.components.camera import Camera, CameraClient
from viam.logging import getLogger
from viam.media.video import CameraMimeType, ViamImage
from viam.module.types import Reconfigurable
from viam.proto.app.robot import ServiceConfig
from viam.proto.common import PointCloudObject, ResourceName
from viam.proto.service.vision import Classification, Detection
from viam.resource.base import ResourceBase
from viam.resource.types import Model, ModelFamily
from viam.services.mlmodel import MLModel
from viam.services.vision import CaptureAllResult, Vision
from viam.utils import ValueTypes

from src.config.config import TrackerConfig
from src.test.fake_embedder_ml_model_service import FakeEmbedderMLModel
from src.tracker.detector.custom_vision_service_detector import (
    CustomVisionServiceDetector,
)
from src.tracker.detector.detector import Detector
from src.tracker.detector.torchvision_detector import TorchvisionDetector
from src.tracker.embedder.custom_mlmodel_service_embedder import (
    CustomMLModelServiceEmbedder,
)
from src.tracker.embedder.embedder import Embedder
from src.tracker.tracker import Tracker

LOGGER = getLogger(__name__)


class TrackerService(Vision, Reconfigurable):
    """TrackerService is a subclass a Viam Vision Service"""

    MODEL: ClassVar[Model] = Model(
        ModelFamily("viam", "vision"), "camera-object-tracking-service"
    )

    def __init__(self, name: str):
        super().__init__(name=name)
        self.camera: CameraClient = None
        self.detector: Detector = None
        self.embedder: Embedder = None
        self.tracker = None

    @classmethod
    def new_service(
        cls, config: ServiceConfig, dependencies: Mapping[ResourceName, ResourceBase]
    ) -> Self:
        """returns new vision service"""
        service = cls(config.name)
        service.reconfigure(config, dependencies)
        return service

    # Validates JSON Configuration
    @classmethod
    def validate_config(cls, config: ServiceConfig) -> Sequence[str]:
        """Validate config and returns a list of dependencies."""
        dependencies = []
        camera_name = config.attributes.fields["camera_name"].string_value
        dependencies.append(camera_name)
        detector_name = config.attributes.fields["detector_name"].string_value
        if detector_name:
            dependencies.append(detector_name)
        embedder_name = config.attributes.fields["embedder_name"].string_value
        if embedder_name:
            dependencies.append(embedder_name)

        # validate the config
        _ = TrackerConfig(config)
        return dependencies

    def reconfigure(
        self, config: ServiceConfig, dependencies: Mapping[ResourceName, ResourceBase]
    ):
        tracker_cfg = TrackerConfig(config)
        self.camera_name = config.attributes.fields["camera_name"].string_value
        self.camera = dependencies[Camera.get_resource_name(self.camera_name)]
        detector_name = config.attributes.fields["detector_name"].string_value

        if not detector_name:
            LOGGER.warning("No detector name provided, using default detector")
            self.detector = TorchvisionDetector(tracker_cfg.detector_config)
        else:
            vision_service = dependencies[Vision.get_resource_name(detector_name)]
            self.detector = CustomVisionServiceDetector(
                tracker_cfg.detector_config, vision_service
            )
        embedder_name = config.attributes.fields["embedder_name"].string_value
        if not embedder_name:
            LOGGER.warning(
                "No embedder name provided, using default embedder"
            )  # TODO: change this when we have a default embedder
            ml_model_service = FakeEmbedderMLModel("FAKE_NAME")
            self.embedder = CustomMLModelServiceEmbedder(
                tracker_cfg.embedder_config, ml_model_service
            )
        else:
            ml_model_service = dependencies[MLModel.get_resource_name(embedder_name)]
            self.embedder = CustomMLModelServiceEmbedder(
                tracker_cfg.embedder_config, ml_model_service
            )

        if self.tracker is not None:
            create_task(self.stop_and_get_new_tracker(tracker_cfg))
        else:
            self.tracker = Tracker(
                tracker_cfg,
                camera=self.camera,
                detector=self.detector,
                embedder=self.embedder,
            )
            self.tracker.start()

    async def stop_and_get_new_tracker(self, tracker_cfg):
        await self.tracker.stop()
        self.tracker = Tracker(tracker_cfg, camera=self.camera, detector=self.detector)
        self.tracker.start()

    async def get_properties(
        self,
        *,
        extra: Optional[Mapping[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Vision.Properties:
        return Vision.Properties(
            classifications_supported=False,
            detections_supported=True,
            object_point_clouds_supported=False,
        )

    async def capture_all_from_camera(
        self,
        camera_name: str,
        return_image: bool = False,
        return_classifications: bool = False,
        return_detections: bool = False,
        return_object_point_clouds: bool = False,
        *,
        extra: Optional[Mapping[str, Any]] = None,
        timeout: Optional[float] = None,
    ):
        if not camera_name == self.camera_name:
            raise ValueError(
                "The camera_name %s doesn't match the camera_name configured for the tracker: %s."
                % (camera_name, self.camera_name)
            )
        img = None
        if return_image:
            img = await self.camera.get_image(mime_type=CameraMimeType.JPEG)

        detections = None
        if return_detections:
            detections = self.tracker.get_current_detections()

        return CaptureAllResult(image=img, detections=detections)

    async def get_object_point_clouds(
        self,
        camera_name: str,
        *,
        extra: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
        **kwargs,
    ) -> List[PointCloudObject]:
        raise NotImplementedError

    async def get_detections(
        self,
        image: ViamImage,
        *,
        extra: Mapping[str, Any],
        timeout: float,
    ) -> List[Detection]:
        return NotImplementedError

    async def get_classifications(
        self,
        image: ViamImage,
        count: int,
        *,
        extra: Optional[Mapping[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> List[Classification]:
        return NotImplementedError

    async def get_classifications_from_camera(
        self,
        camera_name: str,
        count: int,
        *,
        extra: Optional[Mapping[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> List[Classification]:
        return NotImplementedError

    async def get_detections_from_camera(
        self, camera_name: str, *, extra: Mapping[str, Any], timeout: float
    ) -> List[Detection]:
        if camera_name != self.camera_name and camera_name != "":
            raise ValueError(
                "The camera_name doesn't match the camera_name configured for the tracker."
            )
        return self.tracker.get_current_detections()

    async def do_command(
        self,
        command: Mapping[str, ValueTypes],
        *,
        timeout: Optional[float] = None,
        **kwargs,
    ):
        do_command_output = {}
        return do_command_output

    async def close(self):
        """Safely shut down the resource and prevent further use.

        Close must be idempotent. Later configuration may allow a resource to be "open" again.
        If a resource does not want or need a close function, it is assumed that the resource does not need to return errors when future
        non-Close methods are called.

        ::

            await component.close()

        """
        await self.tracker.stop()
        await super().close()
        return
