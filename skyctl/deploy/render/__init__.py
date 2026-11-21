"""Renderers: DeploySpec → target artifacts. One function per target."""

from __future__ import annotations

from ..spec import DeploySpec
from .aws import render_aws
from .azure import render_azure
from .base import Artifact, Finding
from .compose import render_compose
from .gcp import render_gcp
from .kubernetes import render_kubernetes

RENDERERS = {
    "aws": render_aws,
    "azure": render_azure,
    "gcp": render_gcp,
    "kubernetes": render_kubernetes,
    "compose": render_compose,
}


def render(spec: DeploySpec, target: str) -> Artifact:
    if target not in RENDERERS:
        raise ValueError(f"Unknown target {target!r}. Known: {', '.join(RENDERERS)}")
    if getattr(spec.targets, target) is None:
        raise ValueError(f"Target {target!r} is not configured in [targets.{target}]")
    return RENDERERS[target](spec)


__all__ = ["Artifact", "Finding", "RENDERERS", "render"]
