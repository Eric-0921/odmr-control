"""Vector field calculator for spherical ↔ Cartesian magnetic field conversion.

Physics convention: θ (theta) is the polar angle from the Z-axis (0°–180°),
φ (phi) is the azimuthal angle in the XY-plane from the X-axis (0°–360°).

Conversion formulas:
    Bx = B · sin(θ) · cos(φ)
    By = B · sin(θ) · sin(φ)
    Bz = B · cos(θ)
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class SphericalField:
    """Spherical coordinate description of a magnetic field."""

    magnitude_nT: float = 0.0  # field strength B
    theta_deg: float = 0.0     # polar angle θ (from Z-axis, 0–180°)
    phi_deg: float = 0.0       # azimuthal angle φ (from X-axis in XY-plane, 0–360°)


@dataclass
class CartesianField:
    """Cartesian coordinate description of a magnetic field."""

    x_nT: float = 0.0
    y_nT: float = 0.0
    z_nT: float = 0.0


def spherical_to_cartesian(s: SphericalField) -> CartesianField:
    """Convert spherical coordinates to Cartesian (physics convention)."""
    B = s.magnitude_nT
    theta = math.radians(s.theta_deg)
    phi = math.radians(s.phi_deg)
    return CartesianField(
        x_nT=B * math.sin(theta) * math.cos(phi),
        y_nT=B * math.sin(theta) * math.sin(phi),
        z_nT=B * math.cos(theta),
    )


def cartesian_to_spherical(c: CartesianField) -> SphericalField:
    """Convert Cartesian coordinates to spherical (physics convention)."""
    B = math.sqrt(c.x_nT ** 2 + c.y_nT ** 2 + c.z_nT ** 2)
    if B == 0:
        return SphericalField(0.0, 0.0, 0.0)
    theta = math.degrees(math.acos(max(-1.0, min(1.0, c.z_nT / B))))
    phi = math.degrees(math.atan2(c.y_nT, c.x_nT)) % 360.0
    return SphericalField(B, theta, phi)
