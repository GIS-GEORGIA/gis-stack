"""GeoServer manager (Phase 4, feature flag `geoserver_manager`)."""
from . import gateway, manager
from .gateway import CurlGateway, Gateway, Response, for_config
from .manager import GeoServerError

__all__ = ["CurlGateway", "Gateway", "GeoServerError", "Response", "for_config", "gateway", "manager"]
