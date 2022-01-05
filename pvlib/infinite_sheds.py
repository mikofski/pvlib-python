r"""
Infinite Sheds
==============

The "infinite sheds" model is a 2-dimensional model of an array that assumes
rows are long enough that edge effects are negligible and therefore can be
treated as infinite. The infinte sheds model considers an array of adjacent
rows of PV modules versus just a single row. It is also capable of considering
both mono and bifacial modules. Sheds are defined as either fixed tilt or
trackers with uniform GCR on a horizontal plane. To consider arrays on an
non-horizontal planes, rotate the solar vector into the reference frame of the
sloped plane. The main purpose of the infinite shdes model is to modify the
plane of array irradiance components to account for adjancent rows that reduce
incident irradiance on the front and back sides versus a single isolated row
that can see the entire sky as :math:`(1+cos(\beta))/2` and ground as
:math:`(1-cos(\beta))/2`.

Therefore the model picks up after the transposition of diffuse and direct onto
front and back surfaces with the following steps:
1. Find the fraction of unshaded ground between rows, ``f_gnd_beam``. We assume
   there is no direct irradiance in the shaded fraction ``1 - f_gnd_beam``.
2. Calculate the view factor,``fz_sky``, of diffuse sky incident on ground
   between rows and not blocked by adjacent rows. This differs from the single
   row model which assumes ``f_gnd_beam = 1`` or that the ground can see the
   entire sky, and therefore, the ground reflected is just the product of GHI
   and albedo. Note ``f_gnd_beam`` also considers the diffuse sky visible
   between neighboring rows in front and behind the current row. If rows are
   higher off the ground, then the sky might be visible between multiple rows!
3. Calculate the view factor of the ground reflected irradiance incident on PV
    surface.
4. Find the fraction of PV surface shaded. We assume only diffuse in the shaded
   fraction. We treat these two sections differently and assume that the view
   factors of the sky and ground are linear in each section.
5. Sum up components of diffuse sky, diffuse ground, and direct on the front
   and  back PV surfaces.
6. Apply the bifaciality factor to the backside and combine with the front.
7. Treat the first and last row differently, because they aren't blocked on the
   front side for 1st row, or the backside for last row.

That's it folks! This model is influenced by the 2D model published by Marion,
*et al.* in [1].

References
----------
[1] A Practical Irradiance Model for Bifacial PV Modules, Bill Marion, et al.,
IEEE PVSC 2017
[2] Bifacial Performance Modeling in Large Arrays, Mikofski, et al., IEEE PVSC
2018
"""

from collections import OrderedDict
import numpy as np
import pandas as pd
from pvlib import irradiance, pvsystem, iam

EPS = 1e-3  # cos(0.001) = 0.999999999 so v. close to unity
MAXROWS = 15  # assumes 40% GCR and modules 1-m high

def solar_projection(solar_zenith, solar_azimuth, system_azimuth):
    """
    Calculate solar projection on YZ-plane, vertical and perpendicular to rows.

    .. math::
        \\tan \\phi = \\frac{\\cos\\left(\\text{solar azimuth} -
        \\text{system azimuth}\\right)\\sin\\left(\\text{solar zenith}
        \\right)}{\\cos\\left(\\text{solar zenith}\\right)}

    Parameters
    ----------
    solar_zenith : numeric
        apparent zenith in radians
    solar_azimuth : numeric
        azimuth in radians
    system_azimuth : numeric
        system rotation from north in radians

    Returns
    -------
    phi : numeric
        4-quadrant arc-tangent of solar projection in radians
    tan_phi : numeric
        tangent of the solar projection
    """
    rotation = solar_azimuth - system_azimuth
    x1 = np.cos(rotation) * np.sin(solar_zenith)
    x2 = np.cos(solar_zenith)
    tan_phi = x1 / x2
    phi = np.arctan2(x1, x2)
    return phi, tan_phi


def solar_projection_tangent(solar_zenith, solar_azimuth, system_azimuth):
    """
    Calculate tangent of solar projected angle on YZ-plane, vertical and
    perpendicular to rows.

    .. math::
        \\tan \\phi = \\cos\\left(\\text{solar azimuth}-\\text{system azimuth}
        \\right)\\tan\\left(\\text{solar zenith}\\right)

    Parameters
    ----------
    solar_zenith : numeric
        apparent zenith in radians
    solar_azimuth : numeric
        azimuth in radians
    system_azimuth : numeric
        system rotation from north in radians

    Returns
    -------
    tan_phi : numeric
        tangent of the solar projection
    """
    rotation = solar_azimuth - system_azimuth
    tan_phi = np.cos(rotation) * np.tan(solar_zenith)
    return tan_phi


def unshaded_ground_fraction(gcr, tilt, tan_phi):
    """
    Calculate the fraction of the ground with incident direct irradiance.

    .. math::
        F_{gnd,sky} &= 1 - \\min{\\left(1, \\text{GCR} \\left|\\cos \\beta +
        \\sin \\beta \\tan \\phi \\right|\\right)} \\newline

        \\beta &= \\text{tilt}

    Parameters
    ----------
    gcr : numeric
        ratio of module length to row spacing
    tilt : numeric
        angle of module normal from vertical in radians, if bifacial use front
    tan_phi : numeric
        solar projection tangent

    Returns
    -------
    f_gnd_beam : numeric
        fraction of ground illuminated (unshaded)
    """
    f_gnd_beam = 1.0 - np.minimum(
        1.0, gcr * np.abs(np.cos(tilt) + np.sin(tilt) * tan_phi))
    return f_gnd_beam  # 1 - min(1, abs()) < 1 always


def _gcr_prime(gcr, height, tilt, pitch):
    """
    A parameter that includes the distance from the module lower edge to the
    point where the module tilt angle intersects the ground in the GCR.

    Parameters
    ----------
    gcr : numeric
        ground coverage ratio
    height : numeric
        height of module lower edge above the ground
    tilt : numeric
        module tilt in radians, between 0 and 180-degrees
    pitch : numeric
        row spacing

    Returns
    -------
    gcr_prime : numeric
        ground coverage ratio including height above ground
    """

    #  : \\                      \\
    #  :  \\                      \\
    #  :   \\ H = module length    \\
    #  :    \\                      \\
    #  :.....\\......................\\........ module lower edge
    #  :       \                       \    :
    #  :        \                       \   h = height above ground
    #  :         \                 tilt  \  :
    #  +----------\<---------P----------->\---- ground

    return gcr + height / np.sin(tilt) / pitch


def ground_sky_angles(f_z, gcr, height, tilt, pitch):
    """
    Angles from point z on ground to tops of next and previous rows.

    .. math::
        \\tan{\\psi_0} = \\frac{\\sin{\\beta^\\prime}}{\\frac{F_z}
        {\\text{GCR}^\\prime} + \\cos{\\beta^\\prime}}

        \\tan{\\psi_1} = \\frac{\\sin{\\beta}}{\\frac{F_z^\\prime}
        {\\text{GCR}^\\prime} + \\cos{\\beta}}

    Parameters
    ----------
    f_z : numeric
        fraction of ground from previous to next row
    gcr : numeric
        ground coverage ratio
    height : numeric
        height of module lower edge above the ground
    tilt : numeric
        module tilt in radians, between 0 and 180-degrees
    pitch : numeric
        row spacing

    Assuming the first row is in the front of the array then previous rows are
    toward the front of the array and next rows are toward the back.

    """

    #  : \\*                    |\\             front of array
    #  :  \\ **                 | \\
    # next \\   **               | \\ previous row
    # row   \\     **            |  \\
    #  :.....\\.......**..........|..\\........ module lower edge
    #  :       \         **       |    \    :
    #  :        \           **     |    \   h = height above ground
    #  :   tilt  \      psi1   **  |psi0 \  :
    #  +----------\<---------P----*+----->\---- ground
    #             1<-----1-fz-----><--fz--0---- fraction of ground

    # if tilt is close to zero, then these angles don't exist!
    if tilt < EPS or tilt > (np.pi-EPS):
        return np.zeros_like(f_z), np.zeros_like(f_z)
    gcr_prime = _gcr_prime(gcr, height, tilt, pitch)
    tilt_prime = np.pi - tilt
    opposite_side = np.sin(tilt_prime)
    adjacent_side = f_z/gcr_prime + np.cos(tilt_prime)
    # tan_psi_0 = opposite_side / adjacent_side
    psi_0 = np.arctan2(opposite_side, adjacent_side)
    f_z_prime = 1 - f_z
    opposite_side = np.sin(tilt)
    adjacent_side = f_z_prime/gcr_prime + np.cos(tilt)
    # tan_psi_1 = opposite_side / adjacent_side
    psi_1 = np.arctan2(opposite_side, adjacent_side)
    return psi_0, psi_1


def ground_sky_angles_prev(f_z, gcr, height, tilt, pitch):
    """
    Angles from point z on ground to top and bottom of previous rows beyond the
    current row.

    .. math::

        \\tan{\\psi_0} = \\frac{\\sin{\\beta^\\prime}}{\\frac{F_z}
        {\\text{GCR}^\\prime} + \\cos{\\beta^\\prime}}

        0 < F_z < F_{z0,limit}

        \\tan \\psi_1 = \\frac{h}{\\frac{h}{\\tan\\beta} - z}

    Parameters
    ----------
    f_z : numeric
        fraction of ground from previous to next row
    gcr : numeric
        ground coverage ratio
    height : numeric
        height of module lower edge above the ground
    tilt : numeric
        module tilt in radians, between 0 and 180-degrees
    pitch : numeric
        row spacing

    The sky is visible between rows beyond the current row. Therefore, we need
    to calculate the angles :math:`\\psi_0` and :math:`\\psi_1` to the top and
    bottom of the previous row.
    """

    #  : \\        |            *\\ top of previous row
    #  :  \\      |          **   \\
    # prev \\    |         *       \\           front of array
    # row   \\  |       **          \\
    # bottom.\\|......*..............\\........ module lower edge
    #  :      |\   **                  \    :
    #  psi1  |  \* psi0                 \   h = height above ground
    #  :    | ** \                       \  :
    #  +---+*-----\<---------P----------->\---- ground
    #      <-1+fz-1<---------fz=1---------0---- fraction of ground

    gcr_prime = _gcr_prime(gcr, height, tilt, pitch)
    tilt_prime = np.pi - tilt
    z = f_z*pitch
    # if tilt is close to zero, then gcr-prime is infinite
    # so use edges of modules as R2R origin instead
    if tilt < EPS or tilt > (np.pi-EPS):
        psi_1 = np.arctan2(height, -z)
        psi_0 = np.arctan2(
            gcr*np.sin(tilt_prime) + height/pitch, (1+f_z) + gcr*np.cos(tilt_prime))
        return psi_0, psi_1
    # angle to top of previous panel beyond the current row
    psi_0 = np.arctan2(
        np.sin(tilt_prime), (1+f_z)/gcr_prime + np.cos(tilt_prime))
    # angle to bottom of previous panel
    # other forms raise division by zero errors
    # avoid division by zero errors
    psi_1 = np.arctan2(height, height/np.tan(tilt) - z)
    return psi_0, psi_1


def f_z0_limit(gcr, height, tilt, pitch):
    """
    Limit from the ground where sky is visible between previous rows.

    .. math::
        F_{z0,limit} = \\frac{h}{P} \\left(
        \\frac{1}{\\tan \\beta} + \\frac{1}{\\tan \\psi_t}\\right)

    Parameters
    ----------
    gcr : numeric
        ground coverage ratio
    height : numeric
        height of module lower edge above the ground
    tilt : numeric
        module tilt in radians, between 0 and 180-degrees
    pitch : numeric
        row spacing

    The point on the ground, :math:`z_0`, from which the sky is still visible
    between previous rows, where the angle :math:`\\psi` is tangent to both the
    top and bottom of panels.
    """
    # if tilt is zero, then tan_psi_top is also zero, and fz limit is inf
    if tilt < EPS or tilt > (np.pi-EPS):
        return MAXROWS * height/pitch  # scale proportional to height
    tan_psi_t_x0 = sky_angle_0_tangent(gcr, tilt)
    # tan_psi_t_x0 = gcr * np.sin(tilt) / (1.0 - gcr * np.cos(tilt))
    return height/pitch * (1/np.tan(tilt) + 1/tan_psi_t_x0)


def ground_sky_angles_next(f_z, gcr, height, tilt, pitch):
    """
    Angles from point z on the ground to top and bottom of next row beyond
    current row.

    .. math::
        \\tan \\psi_0 = \\frac{h}{\\frac{h}{\\tan\\beta^\\prime}
        - \\left(P-z\\right)}

        \\tan{\\psi_1} = \\frac{\\sin{\\beta}}
        {\\frac{F_z^\\prime}{\\text{GCR}^\\prime} + \\cos{\\beta}}

    Parameters
    ----------
    f_z : numeric
        fraction of ground from previous to next row
    gcr : numeric
        ground coverage ratio
    height : numeric
        height of module lower edge above the ground
    tilt : numeric
        module tilt in radians, between 0 and 180-degrees
    pitch : numeric
        row spacing

    The sky is visible between rows beyond the current row. Therefore, we need
    to calculate the angles :math:`\\psi_0` and :math:`\\psi_1` to the top and
    bottom of the next row.
    """

    #  : \\+                     \\
    #  :  \\  `*+                 \\
    # next \\      `*+             \\
    # row   \\          `*+         \\ next row bottom
    # top....\\..............`*+.....\\_
    #  :       \                  `*+  \ -_  psi0
    #  :        \                psi1  `*+  -_
    #  :         \                       \  `*+ _
    #  +----------\<---------P----------->\------*----- ground
    #             1<---------fz=1---------0-1-fz->----- fraction of ground

    gcr_prime = _gcr_prime(gcr, height, tilt, pitch)
    tilt_prime = np.pi - tilt
    # angle to bottom of next panel
    fzprime = 1-f_z
    zprime = fzprime*pitch
    # if tilt is close to zero, then gcr-prime is infinite
    # so use edges of modules as R2R origin instead
    if tilt < EPS or tilt > (np.pi-EPS):
        psi_0 = np.arctan2(height, -zprime)
        psi_1 = np.arctan2(
            gcr*np.sin(tilt) + height/pitch, (1+fzprime) + gcr*np.cos(tilt))
    # other forms raise division by zero errors
    # avoid division by zero errors
    psi_0 = np.arctan2(height, height/np.tan(tilt_prime) - zprime)
    # angle to top of next panel beyond the current row
    psi_1 = np.arctan2(np.sin(tilt), (1+fzprime)/gcr_prime + np.cos(tilt))
    return psi_0, psi_1


def f_z1_limit(gcr, height, tilt, pitch):
    """
    Limit from the ground where sky is visible between the next rows.

    .. math::
        F_{z1,limit} = \\frac{h}{P} \\left(
        \\frac{1}{\\tan \\psi_t} - \\frac{1}{\\tan \\beta}\\right)

    Parameters
    ----------
    gcr : numeric
        ground coverage ratio
    height : numeric
        height of module lower edge above the ground
    tilt : numeric
        module tilt in radians, between 0 and 180-degrees
    pitch : numeric
        row spacing

    The point on the ground, :math:`z_1^\\prime`, from which the sky is still
    visible between the next rows, where the angle :math:`\\psi` is tangent to
    both the top and bottom of panels.
    """
    # if tilt is zero, then tan_psi_top is also zero, and fz limit is inf
    if tilt < EPS or tilt > (np.pi-EPS):
        return MAXROWS * height/pitch  # scale proportional to height
    tan_psi_t_x1 = sky_angle_0_tangent(gcr, np.pi-tilt)
    # tan_psi_t_x1 = gcr * np.sin(pi-tilt) / (1.0 - gcr * np.cos(pi-tilt))
    return height/pitch * (1/tan_psi_t_x1 - 1/np.tan(tilt))


def calc_fz_sky(psi_0, psi_1):
    """
    Calculate the view factor for point "z" on the ground to the visible
    diffuse sky subtende by the angles :math:`\\psi_0` and :math:`\\psi_1`.

    Parameters
    ----------
    psi_0 : numeric
        angle from ground to sky before point "z"
    psi_1 : numeric
        angle from ground to sky after point "z"

    Returns
    -------
    fz_sky : numeric
        fraction of energy from the diffuse sky dome that is incident on the
        ground at point "z"
    """
    return (np.cos(psi_0) + np.cos(psi_1))/2


# TODO: add argument to set number of rows, default is infinite
# TODO: add option for first or last row, default is middle row
def ground_sky_diffuse_view_factor(gcr, height, tilt, pitch, npoints=100):
    """
    Calculate the fraction of diffuse irradiance from the sky incident on the
    ground.

    Parameters
    ----------
    gcr : numeric
        ground coverage ratio
    height : numeric
        height of module lower edge above the ground
    tilt : numeric
        module tilt in radians, between 0 and 180-degrees
    pitch : numeric
        row spacing
    npoints : int
        divide the ground into discrete points
    """
    args = gcr, height, tilt, pitch
    fz0_limit = f_z0_limit(*args)
    fz1_limit = f_z1_limit(*args)
    # include extra space to account for sky visible from adjacent rows
    # divide ground between visible limits into 3x npoints
    fz = np.linspace(
        0.0 if (1-fz1_limit) > 0 else (1-fz1_limit),
        1.0 if fz0_limit < 1 else fz0_limit,
        3*npoints)
    # calculate the angles psi_0 and psi_1 that subtend the sky visible
    # from between rows
    psi_z = ground_sky_angles(fz, *args)
    # front edge
    psi_z0 = ground_sky_angles_prev(fz, *args)
    fz_sky_next = calc_fz_sky(*psi_z0)
    fz0_sky_next = []
    prev_row = 0.0
    # loop over rows by adding 1.0 to fz until prev_row < ceil(fz0_limit)
    while (fz0_limit - prev_row) > 0:
        fz0_sky_next.append(np.interp(fz + prev_row, fz, fz_sky_next))
        prev_row += 1.0
    # back edge
    psi_z1 = ground_sky_angles_next(fz, *args)
    fz_sky_prev = calc_fz_sky(*psi_z1)
    fz1_sky_prev = []
    next_row = 0.0
    # loop over rows by subtracting 1.0 to fz until next_row < ceil(fz1_limit)
    while (fz1_limit - next_row) > 0:
        fz1_sky_prev.append(np.interp(fz - next_row, fz, fz_sky_prev))
        next_row += 1.0
    # calculate the view factor of the sky from the ground at point z
    fz_sky = (
            calc_fz_sky(*psi_z)  # current row
            + np.sum(fz0_sky_next, axis=0)  # sum of all next rows
            + np.sum(fz1_sky_prev, axis=0))  # sum of all previous rows
    # we just need one row, fz in range [0, 1]
    fz_row = np.linspace(0, 1, npoints)
    return fz_row, np.interp(fz_row, fz, fz_sky)


def vf_ground_sky(gcr, height, tilt, pitch, npoints=100):
    """
    Integrated view factor from the ground in between central rows of the sky.

    Parameters
    ----------
    gcr : numeric
        ground coverage ratio
    height : numeric
        height of module lower edge above the ground
    tilt : numeric
        module tilt in radians, between 0 and 180-degrees
    pitch : numeric
        row spacing
    npoints : int
        divide the ground into discrete points

    """
    args = gcr, height, tilt, pitch
    # calculate the view factor of the diffuse sky from the ground between rows
    z_star, fz_sky = ground_sky_diffuse_view_factor(*args, npoints=npoints)

    # calculate the integrated view factor for all of the ground between rows
    fgnd_sky = np.trapz(fz_sky, z_star)

    return fgnd_sky, fz_sky


def calc_fgndpv_zsky(fx, gcr, height, tilt, pitch, npoints=100):
    """
    Calculate the fraction of diffuse irradiance from the sky, reflecting from
    the ground, incident at a point "x" on the PV surface.

    Parameters
    ----------
    fx : numeric
        fraction of PV surface from bottom
    gcr : numeric
        ground coverage ratio
    height : numeric
        height of module lower edge above the ground
    tilt : numeric
        module tilt in radians, between 0 and 180-degrees
    pitch : numeric
        row spacing
    npoints : int
        divide the ground into discrete points
    """
    args = gcr, height, tilt, pitch

    # calculate the view factor of the diffuse sky from the ground between rows
    # and integrate the view factor for all of the ground between rows
    fgnd_sky, _ = vf_ground_sky(*args, npoints=npoints)

    # if fx is zero, point x is at the bottom of the row, psi_x_bottom is zero,
    # and all of the ground is visible, so the view factor is just
    # Fgnd_pv = (1 - cos(tilt)) / 2
    if fx == 0:
        psi_x_bottom = 0.0
    else:
        # how far on the ground can the point x see?
        psi_x_bottom, _ = ground_angle(gcr, tilt, fx)

    # max angle from pv surface perspective
    psi_max = tilt - psi_x_bottom

    fgnd_pv = (1 - np.cos(psi_max)) / 2
    fskyz = fgnd_sky * fgnd_pv
    return fskyz, fgnd_pv


def diffuse_fraction(ghi, dhi):
    """
    ratio of DHI to GHI

    Parameters
    ----------
    ghi : numeric
        global horizontal irradiance (GHI) in W/m^2
    dhi : numeric
        diffuse horizontal irradiance (DHI) in W/m^2

    Returns
    -------
    df : numeric
        diffuse fraction
    """
    return dhi/ghi


def poa_ground_sky(poa_ground, f_gnd_beam, df, vf_gnd_sky):
    """
    transposed ground reflected diffuse component adjusted for ground
    illumination AND accounting for infinite adjacent rows in both directions

    Parameters
    ----------
    poa_ground : numeric
        transposed ground reflected diffuse component in W/m^2
    f_gnd_beam : numeric
        fraction of interrow ground illuminated (unshaded)
    df : numeric
        ratio of DHI to GHI
    vf_gnd_sky : numeric
        fraction of diffuse sky visible from ground integrated from row to row

    Returns
    -------
    poa_gnd_sky : numeric
        adjusted irradiance on modules reflected from ground
    """
    # split the ground into shaded and unshaded sections with f_gnd_beam
    # the shaded sections only see DHI, while unshaded see GHI = DNI*cos(ze)
    # + DHI, the view factor vf_gnd_sky only applies to the shaded sections
    # see Eqn (2) "Practical Irradiance Model for Bifacial PV" Marion et al.
    # unshaded  + (DHI/GHI)*shaded
    # f_gnd_beam + (DHI/GHI)*(1 - f_gnd_beam)
    # f_gnd_beam + df       *(1 - f_gnd_beam)
    # f_gnd_beam + df - df*f_gnd_beam
    # f_gnd_beam - f_gnd_beam*df + df
    # f_gnd_beam*(1 - df)          + df
    # unshaded *(DNI*cos(ze)/GHI) + DHI/GHI
    # only apply diffuse sky view factor to diffuse component (df) incident on
    # ground between rows, not the direct component of the unshaded ground
    df = np.where(np.isfinite(df), df, 0.0)
    return poa_ground * (f_gnd_beam*(1 - df) + df*vf_gnd_sky)


def shade_line(gcr, tilt, tan_phi):
    """
    calculate fraction of module shaded from the bottom

    .. math::
        F_x = \\max \\left( 0, \\min \\left(1 - \\frac{1}{\\text{GCR} \\left(
        \\cos \\beta + \\sin \\beta \\tan \\phi \\right)}, 1 \\right) \\right)

    Parameters
    ----------
    gcr : numeric
        ratio of module length versus row spacing
    tilt : numeric
        angle of surface normal from vertical in radians
    tan_phi : numeric
        solar projection tangent

    Returns
    -------
    f_x : numeric
        fraction of module shaded from the bottom
    """
    f_x = 1.0 - 1.0 / gcr / (np.cos(tilt) + np.sin(tilt) * tan_phi)
    return np.maximum(0.0, np.minimum(f_x, 1.0))


def sky_angle(gcr, tilt, f_x):
    """
    angle from shade line to top of next row

    Parameters
    ----------
    gcr : numeric
        ratio of module length versus row spacing
    tilt : numeric
        angle of surface normal from vertical in radians
    f_x : numeric
        fraction of module shaded from bottom

    Returns
    -------
    psi_top : numeric
        4-quadrant arc-tangent in radians
    tan_psi_top
        tangent of angle from shade line to top of next row
    """
    f_y = 1.0 - f_x
    x1 = f_y * np.sin(tilt)
    x2 = (1/gcr - f_y * np.cos(tilt))
    tan_psi_top = x1 / x2
    psi_top = np.arctan2(x1, x2)
    return psi_top, tan_psi_top


def sky_angle_tangent(gcr, tilt, f_x):
    """
    tangent of angle from shade line to top of next row

    .. math::

        \\tan{\\psi_t} &= \\frac{F_y \\text{GCR} \\sin{\\beta}}{1 - F_y
        \\text{GCR} \\cos{\\beta}} \\newline

        F_y &= 1 - F_x

    Parameters
    ----------
    gcr : numeric
        ratio of module length versus row spacing
    tilt : numeric
        angle of surface normal from vertical in radians
    f_x : numeric
        fraction of module shaded from bottom

    Returns
    -------
    tan_psi_top : numeric
        tangent of angle from shade line to top of next row
    """
    f_y = 1.0 - f_x
    return f_y * np.sin(tilt) / (1/gcr - f_y * np.cos(tilt))


def sky_angle_0_tangent(gcr, tilt):
    """
    tangent of angle to top of next row with no shade (shade line at bottom) so
    :math:`F_x = 0`

    .. math::

        \\tan{\\psi_t\\left(x=0\\right)} = \\frac{\\text{GCR} \\sin{\\beta}}
        {1 - \\text{GCR} \\cos{\\beta}}

    Parameters
    ----------
    gcr : numeric
        ratio of module length to row spacing
    tilt : numeric
        angle of surface normal from vertical in radians

    Returns
    -------
    tan_psi_top_0 : numeric
        tangent angle from bottom, ``x = 0``, to top of next row
    """
    # f_y = 1  b/c x = 0, so f_x = 0
    # tan psi_t0 = GCR * sin(tilt) / (1 - GCR * cos(tilt))
    return sky_angle_tangent(gcr, tilt, 0.0)


def f_sky_diffuse_pv(tilt, tan_psi_top, tan_psi_top_0):
    """
    view factors of sky from shaded and unshaded parts of PV module

    Parameters
    ----------
    tilt : numeric
        angle of surface normal from vertical in radians
    tan_psi_top : numeric
        tangent of angle from shade line to top of next row
    tan_psi_top_0 : numeric
        tangent of angle to top of next row with no shade (shade line at
        bottom)

    Returns
    -------
    f_sky_pv_shade : numeric
        view factor of sky from shaded part of PV surface
    f_sky_pv_noshade : numeric
        view factor of sky from unshaded part of PV surface

    Notes
    -----
    Assuming the view factor various roughly linearly from the top to the
    bottom of the rack, we can take the average to get integrated view factor.
    We'll average the shaded and unshaded regions separately to improve the
    approximation slightly.

    .. math ::
        \\large{F_{sky \\rightarrow shade} = \\frac{ 1 + \\frac{\\cos
        \\left(\\psi_t + \\beta \\right) + \\cos \\left(\\psi_t
        \\left(x=0\\right) + \\beta \\right)}{2}  }{ 1 + \\cos \\beta}}

    The view factor from the top of the rack is one because it's view is not
    obstructed.

    .. math::
        \\large{F_{sky \\rightarrow no\\ shade} = \\frac{1 + \\frac{1 +
        \\cos \\left(\\psi_t + \\beta \\right)}{1 + \\cos \\beta} }{2}}
    """
    # TODO: don't average, return fsky-pv vs. x point on panel
    psi_top = np.arctan(tan_psi_top)
    psi_top_0 = np.arctan(tan_psi_top_0)
    f_sky_pv_shade = (
        (1 + (np.cos(psi_top + tilt)
              + np.cos(psi_top_0 + tilt)) / 2) / (1 + np.cos(tilt)))

    f_sky_pv_noshade = (1 + (
        1 + np.cos(psi_top + tilt)) / (1 + np.cos(tilt))) / 2
    return f_sky_pv_shade, f_sky_pv_noshade


def poa_sky_diffuse_pv(poa_sky_diffuse, f_x, f_sky_pv_shade, f_sky_pv_noshade):
    """
    Sky diffuse POA from average view factor weighted by shaded and unshaded
    parts of the surface.

    Parameters
    ----------
    poa_sky_diffuse : numeric
        sky diffuse irradiance on the plane of array (W/m^2)
    f_x : numeric
        shade line fraction from bottom of module
    f_sky_pv_shade : numeric
        fraction of sky visible from shaded part of PV surface
    f_sky_pv_noshade : numeric
        fraction of sky visible from unshaded part of PV surface

    Returns
    -------
    poa_sky_diffuse_pv : numeric
        total sky diffuse irradiance incident on PV surface
    """
    return poa_sky_diffuse * (f_x*f_sky_pv_shade + (1 - f_x)*f_sky_pv_noshade)


def ground_angle(gcr, tilt, f_x):
    """
    angle from shadeline to bottom of adjacent row

    Parameters
    ----------
    gcr : numeric
        ratio of module length to row spacing
    tilt : numeric
        angle of surface normal from vertical in radians
    f_x : numeric
        fraction of module shaded from bottom, ``f_x = 0`` if shade line at
        bottom and no shade, ``f_x = 1`` if shade line at top and all shade

    Returns
    -------
    psi_bottom : numeric
        4-quadrant arc-tangent
    tan_psi_bottom : numeric
        tangent of angle from shade line to bottom of next row
    """
    x1 = f_x * np.sin(tilt)
    x2 = (f_x * np.cos(tilt) + 1/gcr)
    tan_psi_bottom = x1 / x2
    psi_bottom = np.arctan2(x1, x2)
    return psi_bottom, tan_psi_bottom


def ground_angle_tangent(gcr, tilt, f_x):
    """
    tangent of angle from shadeline to bottom of adjacent row

    .. math::
        \\tan{\\psi_b} = \\frac{F_x \\sin \\beta}{F_x \\cos \\beta +
        \\frac{1}{\\text{GCR}}}

    Parameters
    ----------
    gcr : numeric
        ratio of module length to row spacing
    tilt : numeric
        angle of surface normal from vertical in radians
    f_x : numeric
        fraction of module shaded from bottom, ``f_x = 0`` if shade line at
        bottom and no shade, ``f_x = 1`` if shade line at top and all shade

    Returns
    -------
    tan_psi_bottom : numeric
        tangent of angle from shade line to bottom of next row
    """
    return f_x * np.sin(tilt) / (
        f_x * np.cos(tilt) + 1/gcr)


def ground_angle_1_tangent(gcr, tilt):
    """
    tangent of angle to bottom of next row with all shade (shade line at top)
    so :math:`F_x = 1`

    .. math::
        \\tan{\\psi_b\\left(x=1\\right)} = \\frac{\\sin{\\beta}}{\\cos{\\beta}
        + \\frac{1}{\\text{GCR}}}

    Parameters
    ----------
    gcr : numeric
        ratio of module length to row spacing
    tilt : numeric
        angle of surface normal from vertical in radians

    Returns
    -------
    tan_psi_bottom_1 : numeric
        tangent of angle to bottom of next row with all shade (shade line at
        top)
    """
    return ground_angle_tangent(gcr, tilt, 1.0)


def f_ground_pv(tilt, tan_psi_bottom, tan_psi_bottom_1):
    """
    view factors of ground from shaded and unshaded parts of PV module

    Parameters
    ----------
    tilt : numeric
        angle of surface normal from vertical in radians
    tan_psi_bottom : numeric
        tangent of angle from shade line to bottom of next row
    tan_psi_bottom_1 : numeric
        tangent of angle to bottom of next row with all shade

    Returns
    -------
    f_gnd_pv_shade : numeric
        view factor of ground from shaded part of PV surface
    f_gnd_pv_noshade : numeric
        view factor of ground from unshaded part of PV surface

    Notes
    -----
    At the bottom of rack, :math:`x = 0`, the angle is zero, and the view
    factor is one.

    .. math::
        \\large{F_{gnd \\rightarrow shade} = \\frac{1 + \\frac{1 - \\cos
        \\left(\\beta - \\psi_b \\right)}{1 - \\cos \\beta}}{2}}

    Take the average of the shaded and unshaded sections.

    .. math::
        \\large{F_{gnd \\rightarrow no\\ shade} = \\frac{1 - \\frac{\\cos
        \\left(\\beta - \\psi_b \\right) + \\cos \\left(\\beta - \\psi_b
        \\left(x=1\\right) \\right)}{2}}{1 - \\cos \\beta}}
    """
    # TODO: don't average, return fgnd-pv vs. x point on panel
    psi_bottom = np.arctan(tan_psi_bottom)
    psi_bottom_1 = np.arctan(tan_psi_bottom_1)
    f_gnd_pv_shade = (1 + (1 - np.cos(tilt - psi_bottom))
                      / (1 - np.cos(tilt))) / 2
    f_gnd_pv_noshade = (
        (1 - (np.cos(tilt - psi_bottom) + np.cos(tilt - psi_bottom_1))/2)
        / (1 - np.cos(tilt)))
    return f_gnd_pv_shade, f_gnd_pv_noshade


def poa_ground_pv(poa_gnd_sky, f_x, f_gnd_pv_shade, f_gnd_pv_noshade):
    """
    Ground diffuse POA from average view factor weighted by shaded and unshaded
    parts of the surface.

    Parameters
    ----------
    poa_gnd_sky : numeric
        diffuse ground POA accounting for ground shade but not adjacent rows
    f_x : numeric
        shade line fraction from bottom of module
    f_gnd_pv_shade : numeric
        fraction of ground visible from shaded part of PV surface
    f_gnd_pv_noshade : numeric
        fraction of ground visible from unshaded part of PV surface

    """
    return poa_gnd_sky * (f_x*f_gnd_pv_shade + (1 - f_x)*f_gnd_pv_noshade)


def poa_diffuse_pv(poa_gnd_pv, poa_sky_pv):
    """diffuse incident on PV surface from sky and ground"""
    return poa_gnd_pv + poa_sky_pv


def poa_direct_pv(poa_direct, iam, f_x):
    """direct incident on PV surface"""
    return poa_direct * iam * (1 - f_x)


def poa_global_pv(poa_dir_pv, poa_dif_pv):
    """global incident on PV surface"""
    return poa_dir_pv + poa_dif_pv


def poa_global_bifacial(poa_global_front, poa_global_back, bifaciality=0.8,
                        shade_factor=-0.02, transmission_factor=0):
    """total global incident on bifacial PV surfaces"""
    effects = (1+shade_factor)*(1+transmission_factor)
    return poa_global_front + poa_global_back * bifaciality * effects


def get_irradiance(solar_zenith, solar_azimuth, system_azimuth, gcr, height,
                   tilt, pitch, ghi, dhi, poa_ground, poa_sky_diffuse,
                   poa_direct, iam, npoints=100, all_output=False):
    """Get irradiance from infinite sheds model."""
    # calculate solar projection
    tan_phi = solar_projection_tangent(
        solar_zenith, solar_azimuth, system_azimuth)
    # fraction of ground illuminated accounting from shade from panels
    f_gnd_beam = unshaded_ground_fraction(gcr, tilt, tan_phi)
    # diffuse fraction
    df = diffuse_fraction(ghi, dhi)
    # view factor from the ground in between infinited central rows of the sky
    vf_gnd_sky, _ = vf_ground_sky(gcr, height, tilt, pitch, npoints)
    # diffuse from sky reflected from ground accounting from shade from panels
    # considering the fraction of ground blocked by infinite adjacent rows
    poa_gnd_sky = poa_ground_sky(poa_ground, f_gnd_beam, df, vf_gnd_sky)
    # fraction of panel shaded
    f_x = shade_line(gcr, tilt, tan_phi)
    # angles from shadeline to top of next row
    tan_psi_top = sky_angle_tangent(gcr, tilt, f_x)
    tan_psi_top_0 = sky_angle_0_tangent(gcr, tilt)
    # fraction of sky visible from shaded and unshaded parts of PV surfaces
    f_sky_pv_shade, f_sky_pv_noshade = f_sky_diffuse_pv(
        tilt, tan_psi_top, tan_psi_top_0)
    # total sky diffuse incident on plane of array
    poa_sky_pv = poa_sky_diffuse_pv(
        poa_sky_diffuse, f_x, f_sky_pv_shade, f_sky_pv_noshade)
    # angles from shadeline to bottom of next row
    tan_psi_bottom = ground_angle_tangent(gcr, tilt, f_x)
    tan_psi_bottom_1 = ground_angle_1_tangent(gcr, tilt)
    f_gnd_pv_shade, f_gnd_pv_noshade = f_ground_pv(
        tilt, tan_psi_bottom, tan_psi_bottom_1)
    poa_gnd_pv = poa_ground_pv(
        poa_gnd_sky, f_x, f_gnd_pv_shade, f_gnd_pv_noshade)
    poa_dif_pv = poa_diffuse_pv(poa_gnd_pv, poa_sky_pv)
    poa_dir_pv = poa_direct_pv(poa_direct, iam, f_x)
    poa_glo_pv = poa_global_pv(poa_dir_pv, poa_dif_pv)
    output = OrderedDict(
        poa_global_pv=poa_glo_pv, poa_direct_pv=poa_dir_pv,
        poa_diffuse_pv=poa_dif_pv, poa_ground_diffuse_pv=poa_gnd_pv,
        poa_sky_diffuse_pv=poa_sky_pv)
    if all_output:
        output.update(
            solar_projection=tan_phi, ground_illumination=f_gnd_beam,
            diffuse_fraction=df, poa_ground_sky=poa_gnd_sky, shade_line=f_x,
            sky_angle_tangent=tan_psi_top, sky_angle_0_tangent=tan_psi_top_0,
            f_sky_diffuse_pv_shade=f_sky_pv_shade,
            f_sky_diffuse_pv_noshade=f_sky_pv_noshade,
            ground_angle_tangent=tan_psi_bottom,
            ground_angle_1_tangent=tan_psi_bottom_1,
            f_ground_diffuse_pv_shade=f_gnd_pv_shade,
            f_ground_diffuse_pv_noshade=f_gnd_pv_noshade)
    if isinstance(poa_glo_pv, pd.Series):
        output = pd.DataFrame(output)
    return output


def get_poa_global_bifacial(solar_zenith, solar_azimuth, system_azimuth, gcr,
                            height, tilt, pitch, ghi, dhi, dni, dni_extra,
                            am_rel, iam_b0_front=0.05, iam_b0_back=0.05,
                            bifaciality=0.8, shade_factor=-0.02,
                            transmission_factor=0, method='haydavies'):
    """Get global bifacial irradiance from infinite sheds model."""
    # backside is rotated and flipped relative to front
    backside_tilt, backside_sysaz = _backside(tilt, system_azimuth)
    # AOI
    aoi_front = irradiance.aoi(
        tilt, system_azimuth, solar_zenith, solar_azimuth)
    aoi_back = irradiance.aoi(
        backside_tilt, backside_sysaz, solar_zenith, solar_azimuth)
    # transposition
    irrad_front = irradiance.get_total_irradiance(
        tilt, system_azimuth, solar_zenith, solar_azimuth,
        dni, ghi, dhi, dni_extra, am_rel, model=method)
    irrad_back = irradiance.get_total_irradiance(
        backside_tilt, backside_sysaz, solar_zenith, solar_azimuth,
        dni, ghi, dhi, dni_extra, am_rel, model=method)
    # iam
    iam_front = iam.ashrae(aoi_front, iam_b0_front)
    iam_back = iam.ashrae(aoi_back, iam_b0_back)
    # get front side
    poa_global_front = get_irradiance(
        solar_zenith, solar_azimuth, system_azimuth, gcr, height, tilt, pitch,
        ghi, dhi, irrad_front['poa_ground_diffuse'],
        irrad_front['poa_sky_diffuse'], irrad_front['poa_direct'], iam_front)
    # get backside
    poa_global_back = get_irradiance(
        solar_zenith, solar_azimuth, backside_sysaz, gcr, height,
        backside_tilt, pitch, ghi, dhi, irrad_back['poa_ground_diffuse'],
        irrad_back['poa_sky_diffuse'], irrad_back['poa_direct'], iam_back)
    # get bifacial
    poa_glo_bifi = poa_global_bifacial(
        poa_global_front['poa_global_pv'], poa_global_back['poa_global_pv'],
        bifaciality, shade_factor, transmission_factor)
    return poa_glo_bifi


def _backside(tilt, system_azimuth):
    backside_tilt = np.pi - tilt
    backside_sysaz = (np.pi + system_azimuth) % (2*np.pi)
    return backside_tilt, backside_sysaz


class InfiniteSheds(object):
    """An infinite sheds model"""
    def __init__(self, system_azimuth, gcr, height, tilt, pitch, npoints=100,
                 is_bifacial=True, bifaciality=0.8, shade_factor=-0.02,
                 transmission_factor=0):
        self.system_azimuth = system_azimuth
        self.gcr = gcr
        self.height = height
        self.tilt = tilt
        self.pitch = pitch
        self.npoints = npoints
        self.is_bifacial = is_bifacial
        self.bifaciality = bifaciality if is_bifacial else 0.0
        self.shade_factor = shade_factor
        self.transmission_factor = transmission_factor
        # backside angles
        self.backside_tilt, self.backside_sysaz = _backside(
            self.tilt, self.system_azimuth)
        # sheds parameters
        self.tan_phi = None
        self.f_gnd_beam = None
        self.df = None
        self.front_side = None
        self.back_side = None
        self.poa_global_bifacial = None

    def get_irradiance(self, solar_zenith, solar_azimuth, ghi, dhi, poa_ground,
                       poa_sky_diffuse, poa_direct, iam):
        self.front_side = _PVSurface(*get_irradiance(
            solar_zenith, solar_azimuth, self.system_azimuth,
            self.gcr, self.height, self.tilt, self.pitch, ghi, dhi, poa_ground,
            poa_sky_diffuse, poa_direct, iam, npoints=self.npoints,
            all_output=True))
        self.tan_phi = self.front_side.tan_phi
        self.f_gnd_beam = self.front_side.f_gnd_beam
        self.df = self.front_side.df
        if self.bifaciality > 0:
            self.back_side = _PVSurface(*get_irradiance(
                solar_zenith, solar_azimuth, self.backside_sysaz,
                self.gcr, self.height, self.backside_tilt, self.pitch, ghi,
                dhi, poa_ground, poa_sky_diffuse, poa_direct, iam,
                self.npoints, all_output=True))
            self.poa_global_bifacial = poa_global_bifacial(
                self.front_side.poa_global_pv, self.back_side.poa_global_pv,
                self.bifaciality, self.shade_factor, self.transmission_factor)
            return self.poa_global_bifacial
        else:
            return self.front_side.poa_global_pv


class _PVSurface(object):
    """A PV surface in an infinite shed"""
    def __init__(self, poa_glo_pv, poa_dir_pv, poa_dif_pv, poa_gnd_pv,
                 poa_sky_pv, tan_phi, f_gnd_beam, df, poa_gnd_sky, f_x,
                 tan_psi_top, tan_psi_top_0, f_sky_pv_shade, f_sky_pv_noshade,
                 tan_psi_bottom, tan_psi_bottom_1, f_gnd_pv_shade,
                 f_gnd_pv_noshade):
        self.poa_global_pv = poa_glo_pv
        self.poa_direct_pv = poa_dir_pv
        self.poa_diffuse_pv = poa_dif_pv
        self.poa_ground_pv = poa_gnd_pv
        self.poa_sky_diffuse_pv = poa_sky_pv
        self.tan_phi = tan_phi
        self.f_gnd_beam = f_gnd_beam
        self.df = df
        self.poa_ground_sky = poa_gnd_sky
        self.f_x = f_x
        self.tan_psi_top = tan_psi_top
        self.tan_psi_top_0 = tan_psi_top_0
        self.f_sky_pv_shade = f_sky_pv_shade
        self.f_sky_pv_noshade = f_sky_pv_noshade
        self.tan_psi_bottom = tan_psi_bottom
        self.tan_psi_bottom_1 = tan_psi_bottom_1
        self.f_gnd_pv_shade = f_gnd_pv_shade
        self.f_gnd_pv_noshade = f_gnd_pv_noshade
