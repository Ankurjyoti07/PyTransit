#  PyTransit: fast and easy exoplanet transit modelling in Python.
#  Copyright (C) 2010-2020  Hannu Parviainen
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program.  If not, see <https://www.gnu.org/licenses/>.
from numba import njit, prange
from numpy import arccos, sqrt, linspace, zeros, arange, dot, floor, pi, ndarray, atleast_1d, atleast_2d, isnan, inf, \
    atleast_3d, nan

from pytransit.orbits.orbits_py import z_ip_s, z_ip_v


@njit
def circle_circle_intersection_area(r1, r2, b):
    """Area of the intersection of two circles.
    """
    if r1 < b - r2:
        return 0.0
    elif r1 >= b + r2:
        return pi * r2 ** 2
    elif b - r2 <= -r1:
        return pi * r1 ** 2
    else:
        return (r2 ** 2 * arccos((b ** 2 + r2 ** 2 - r1 ** 2) / (2 * b * r2)) +
                r1 ** 2 * arccos((b ** 2 + r1 ** 2 - r2 ** 2) / (2 * b * r1)) -
                0.5 * sqrt((-b + r2 + r1) * (b + r2 - r1) * (b - r2 + r1) * (b + r2 + r1)))


@njit
def circle_circle_intersection_area_v(r1, r2, bs):
    """Area of the intersection of two circles.
    """
    a = zeros(bs.size)
    for i in range(bs.size):
        b = bs[i]
        if r1 < b - r2:
            a[i] = 0.0
        elif r1 >= b + r2:
            a[i] = pi * r2 ** 2
        elif b - r2 <= -r1:
            a[i] = pi * r1 ** 2
        else:
            a[i] = (r2 ** 2 * arccos((b ** 2 + r2 ** 2 - r1 ** 2) / (2 * b * r2)) +
                    r1 ** 2 * arccos((b ** 2 + r1 ** 2 - r2 ** 2) / (2 * b * r1)) -
                    0.5 * sqrt((-b + r2 + r1) * (b + r2 - r1) * (b - r2 + r1) * (b + r2 + r1)))
    return a

@njit
def create_z_grid(zcut: float, nin: int, nedge: int):
    mucut = sqrt(1.0 - zcut ** 2)
    dz = zcut / nin
    dmu = mucut / nedge

    z_edges = zeros(nin + nedge)
    z_means = zeros(nin + nedge)

    for i in range(nin - 1):
        z_edges[i] = (i + 1) * dz

    for i in range(nedge + 1):
        z_edges[-i - 1] = sqrt(1 - (i * dmu) ** 2)

    for i in range(nin + nedge - 1):
        z_means[i + 1] = 0.5 * (z_edges[i] + z_edges[i + 1])

    return z_edges, z_means


@njit
def create_z_grid_acos(nz: int):
    z_edges = arccos(linspace(1.0, 0.0, nz)) / (0.5*pi)
    z_means = zeros(nz)
    for i in range(nz - 1):
        z_means[i + 1] = 0.5 * (z_edges[i] + z_edges[i + 1])
    return z_edges, z_means


@njit
def calculate_weights_2d(k: float, ze: ndarray, ng: int):
    """Calculate a 2D limb darkening weight array.

    Parameters
    ----------
    k: float
        Radius ratio
    ng: int
        Grazing parameter resolution
    nmu: int
        Mu resolution

    Returns
    -------

    """
    gs = linspace(0, 1 - 1e-7, ng)
    nz = ze.size
    weights = zeros((ng, nz))

    for ig in range(ng):
        b = gs[ig] * (1.0 + k)
        a0 = circle_circle_intersection_area(ze[0], k, b)
        weights[ig, 0] = a0
        s = weights[ig, 0]
        for i in range(1, nz):
            a1 = circle_circle_intersection_area(ze[i], k, b)
            weights[ig, i] = a1 - a0
            a0 = a1
            s += weights[ig, i]
        for i in range(nz):
            weights[ig, i] /= s
    return gs, gs[1] - gs[0], weights


@njit
def calculate_weights_3d(nk: int, k0: float, k1: float, ze: ndarray, ng: int):
    """Calculate a 3D limb darkening weight array.

    Parameters
    ----------
    k: float
        Radius ratio
    ng: int
        Grazing parameter resolution
    nmu: int
        Mu resolution

    Returns
    -------

    """
    ks = linspace(k0, k1, nk)
    gs = linspace(0., 1. - 1e-7, ng)
    nz = ze.size
    weights = zeros((nk,ng, nz))

    for ik in range(nk):
        for ig in range(ng):
            b = gs[ig] * (1.0 + ks[ik])
            a0 = circle_circle_intersection_area(ze[0], ks[ik], b)
            weights[ik, ig, 0] = a0
            s = weights[ik, ig, 0]
            for i in range(1, nz):
                a1 = circle_circle_intersection_area(ze[i], ks[ik], b)
                weights[ik, ig, i] = a1 - a0
                a0 = a1
                s += weights[ik, ig, i]
            for i in range(nz):
                weights[ik, ig, i] /= s
    return (k1-k0)/nk, gs[1] - gs[0], weights


@njit(fastmath=True)
def interpolate_limb_darkening_s(z, mz, ldp):
    if z < 0.0:
        return nan
    if z > mz[-1]:
        return ldp[-1]

    i = mz.size // 2
    if z > mz[i]:
        while z > mz[i + 1]:
            i += 1
    else:
        while z < mz[i]:
            i -= 1

    a = (z - mz[i]) / (mz[i + 1] - mz[i])
    return (1.0 - a) * ldp[i] + a * ldp[i + 1]


@njit(fastmath=True)
def interpolate_limb_darkening_v(zs, mz, ldp):
    nz = zs.size
    ld = zeros(nz)

    for iz in range(nz):
        z = zs[iz]
        if z < 0.0:
            ld[iz] = nan
            continue
        if z > mz[-1]:
            ld[iz] = ldp[-1]
            continue

        i = mz.size // 2
        if z > mz[i]:
            while z > mz[i + 1]:
                i += 1
        else:
            while z < mz[i]:
                i -= 1

        a = (z - mz[i]) / (mz[i + 1] - mz[i])
        ld[iz] = (1.0 - a) * ldp[i] + a * ldp[i + 1]
    return ld


@njit
def lerp(g, dg, ldw):
    if g >= 1.0:
        return 0.0
    else:
        ng = g / dg
        ig = int(floor(ng))
        ag = ng - ig
        return (1.0 - ag) * ldw[ig] + ag * ldw[ig+1]

@njit
def im_p_s(g: float, dg, weights, ldp):
    """The average limb darkening intensity blocked by the planet.

    Parameters
    ----------
    g: float
        Grazing parameter
    dg: float
        Grazing parameter array step size
    weights: ndarray
        Limb darkening weight array
    ldp: ndarray
        Limb darkening values

    Returns
    -------

    """
    if g >= 1.0:
        return 0.0
    else:
        ng = g / dg
        ig = int(floor(ng))
        ag = ng - ig
        return (1.0 - ag) * dot(weights[ig], ldp) + ag * dot(weights[ig + 1], ldp)


@njit
def im_p_s_3d(k: float, g: float, dk, dg, weights, ldp, k0):
    if g >= 1.0:
        return 0.0
    else:
        nk = (k - k0) / dg
        ik = int(floor(nk))
        ak1 = nk - ik
        ak2 = 1.0 - ak1

        ng = g / dg
        ig = int(floor(ng))
        ag1 = ng - ig
        ag2 = 1.0 - ag1

        l00 = dot(weights[ik, ig], ldp)
        l01 = dot(weights[ik, ig + 1], ldp)
        l10 = dot(weights[ik + 1, ig], ldp)
        l11 = dot(weights[ik + 1, ig + 1], ldp)

        return (l00 * ak2 * ag2
                + l10 * ak1 * ag2
                + l01 * ak2 * ag1
                + l11 * ak1 * ag1)

@njit
def im_p_v(gs, dg, weights, ldp):
    """The average limb darkening intensity blocked by the planet.

    Parameters
    ----------
    g: float
        Grazing parameter
    dg: float
        Grazing parameter array step size
    weights: ndarray
        Limb darkening weight array
    ldp: ndarray
        Limb darkening values

    Returns
    -------

    """
    ldw = dot(weights, ldp)
    im = zeros(gs.size)
    for i in range(gs.size):
        if gs[i] >= 1.0:
            im[i] = 0.0
        else:
            ng = gs[i] / dg
            ig = int(floor(ng))
            ag = ng - ig
            im[i] = (1.0 - ag) * ldw[ig] + ag * ldw[ig + 1]
    return im


@njit
def im_p_v2(gs, dg, ldw):
    """The average limb darkening intensity blocked by the planet.

    Parameters
    ----------
    g: float
        Grazing parameter
    dg: float
        Grazing parameter array step size
    weights: ndarray
        Limb darkening weight array
    ldp: ndarray
        Limb darkening values

    Returns
    -------

    """
    im = zeros(gs.size)
    for i in range(gs.size):
        if gs[i] >= 1.0:
            im[i] = 0.0
        else:
            ng = gs[i] / dg
            ig = int(floor(ng))
            ag = ng - ig
            im[i] = (1.0 - ag) * ldw[ig] + ag * ldw[ig + 1]
    return im


@njit(parallel=True)
def swmodel_z_direct_parallel(z, k, istar, ng, ldp, ze):
    flux = zeros(z.size)
    gs, dg, weights = calculate_weights_2d(k, ze, ng)
    ldw = dot(weights, ldp)
    for i in prange(z.size):
        iplanet = lerp(z[i] / (1. + k), dg, ldw)
        aplanet = circle_circle_intersection_area(1.0, k, z[i])
        flux[i] = (istar - iplanet * aplanet) / istar
    return flux

@njit
def swmodel_z_direct_serial(z, k, istar, ng, ldp, ze):
    gs, dg, weights = calculate_weights_2d(k, ze, ng)
    ztog = 1. / (1. + k)
    iplanet = im_p_v(z*ztog, dg, weights, ldp)
    aplanet = circle_circle_intersection_area_v(1.0, k, z)
    return (istar - iplanet * aplanet) / istar

@njit
def swmodel_z_interpolated_serial(z, k, istar, ldp, weights, dk, k0, dg):
    nk = (k - k0) / dk
    ik = int(floor(nk))
    ak = nk - ik
    ldw = (1.0 - ak) * dot(weights[ik], ldp) + ak * dot(weights[ik + 1], ldp)
    ztog = 1. / (1. + k)
    iplanet = im_p_v2(z*ztog, dg, ldw)
    aplanet = circle_circle_intersection_area_v(1.0, k, z)
    return (istar - iplanet * aplanet) / istar


@njit(parallel=True)
def swmodel_z_interpolated_parallel(z, k, istar, ldp, weights, dk, k0, dg):
    flux = zeros(z.size)
    nk = (k - k0) / dk
    ik = int(floor(nk))
    ak = nk - ik
    ldw = (1.0 - ak) * dot(weights[ik], ldp) + ak * dot(weights[ik + 1], ldp)
    ztog = 1. / (1. + k)
    for i in prange(z.size):
        iplanet = lerp(z[i]*ztog, dg, ldw)
        aplanet = circle_circle_intersection_area(1.0, k, z[i])
        flux[i] = (istar - iplanet * aplanet) / istar
    return flux


@njit(parallel=False, fastmath=True)
def swmodel_direct_s_simple(t, k, t0, p, a, i, e, w, ldp, istar, ze, zm, ng, splimit, lcids, pbids, nsamples, exptimes, es, ms, tae, parallel):
    """Simple PT transit model for a homogeneous time series without supersampling and relatively small number of points.

    This version avoids the overheads from threading and supersampling. The fastest option if the number of datapoints
    is smaller than some thousands.
    """
    k = atleast_1d(k)
    z = z_ip_v(t, t0, p, a, i, e, w, es, ms, tae)

    # Swift model branch
    # ------------------
    if k[0] > splimit:
        gs, dg, weights = calculate_weights_2d(k[0], ze, ng)
        ldw = dot(weights, ldp[0,0,:])
        iplanet = im_p_v2(z / (1. + k[0]), dg, ldw)

    # Small-planet approximation branch
    # ---------------------------------
    else:
        iplanet = interpolate_limb_darkening_v(z, zm, ldp[0,0,:])

    aplanet = circle_circle_intersection_area_v(1.0, k[0], z)
    flux = (istar[0,0] - iplanet * aplanet) / istar[0,0]
    return flux

@njit(parallel=False, fastmath=True)
def _eval_s_serial(t, k, t0, p, a, i, e, w, istar, zm, dg, ldp, ldw, splimit, lcids, pbids, nsamples, exptimes, es, ms, tae):
    npt = t.size
    flux = zeros(npt)

    for j in range(npt):
        ilc = lcids[j]
        ipb = pbids[ilc]
        _k = k[0] if k.size == 1 else k[ipb]

        for isample in range(1, nsamples[ilc] + 1):
            time_offset = exptimes[ilc] * ((isample - 0.5) / nsamples[ilc] - 0.5)
            z = z_ip_s(t[j] + time_offset, t0, p, a, i, e, w, es, ms, tae)
            if z > 1.0 + _k:
                flux[j] += 1.
            else:
                if _k > splimit:
                    iplanet = lerp(z / (1. + _k), dg, ldw[ipb])
                else:
                    iplanet = interpolate_limb_darkening_s(z, zm, ldp[0,ipb])
                aplanet = circle_circle_intersection_area(1.0, _k, z)
                flux[j] += (istar[0,ipb] - iplanet * aplanet) / istar[0,ipb]
        flux[j] /= nsamples[ilc]
    return flux


@njit(parallel=True, fastmath=True)
def _eval_s_parallel(t, k, t0, p, a, i, e, w, istar, zm, dg, ldp, ldw, splimit, lcids, pbids, nsamples, exptimes, es, ms, tae):
    npt = t.size
    flux = zeros(npt)

    for j in prange(npt):
        ilc = lcids[j]
        ipb = pbids[ilc]
        _k = k[0] if k.size == 1 else k[ipb]

        for isample in range(1, nsamples[ilc] + 1):
            time_offset = exptimes[ilc] * ((isample - 0.5) / nsamples[ilc] - 0.5)
            z = z_ip_s(t[j] + time_offset, t0, p, a, i, e, w, es, ms, tae)
            if z > 1.0 + _k:
                flux[j] += 1.
            else:
                if _k > splimit:
                    iplanet = lerp(z / (1. + _k), dg, ldw[ipb])
                else:
                    iplanet = interpolate_limb_darkening_s(z, zm, ldp[0,ipb])
                aplanet = circle_circle_intersection_area(1.0, _k, z)
                flux[j] += (istar[0,ipb] - iplanet * aplanet) / istar[0,ipb]
        flux[j] /= nsamples[ilc]
    return flux


@njit
def swmodel_direct_s(t, k, t0, p, a, i, e, w, ldp, istar, ze, zm, ng, splimit, lcids, pbids, nsamples, exptimes, es, ms, tae, parallel):
    k = atleast_1d(k)
    npb = ldp.shape[1]
    gs, dg, weights = calculate_weights_2d(k[0], ze, ng)

    ldw = zeros((npb, ng))
    for ipb in range(npb):
        ldw[ipb] = dot(weights, ldp[0, ipb])

    if parallel:
        return _eval_s_parallel(t, k, t0, p, a, i, e, w, istar, zm, dg, ldp, ldw, splimit, lcids, pbids, nsamples, exptimes, es, ms, tae)
    else:
        return _eval_s_serial(t, k, t0, p, a, i, e, w, istar, zm, dg, ldp, ldw, splimit, lcids, pbids, nsamples, exptimes, es, ms, tae)


@njit(fastmath=True)
def swmodel_interpolated_s(t, k, t0, p, a, i, e, w, ldp, istar, weights, zm, dk, k0, dg, splimit,
                           lcids, pbids, nsamples, exptimes, es, ms, tae, parallel):
    k = atleast_1d(k)
    npb = ldp.shape[1]
    nk = (k[0] - k0) / dk
    ik = int(floor(nk))
    ak = nk - ik

    ldw = zeros((npb, weights.shape[1]))
    for ipb in range(npb):
        ldw[ipb] = (1.0 - ak) * dot(weights[ik], ldp[0,ipb]) + ak * dot(weights[ik + 1], ldp[0,ipb])

    if parallel:
        return _eval_s_parallel(t, k, t0, p, a, i, e, w, istar, zm, dg, ldp, ldw, splimit, lcids, pbids, nsamples, exptimes, es, ms, tae)
    else:
        return _eval_s_serial(t, k, t0, p, a, i, e, w, istar, zm, dg, ldp, ldw, splimit, lcids, pbids, nsamples, exptimes, es, ms, tae)


@njit(parallel=True, fastmath=False)
def swmodel_direct_v(t, k, t0, p, a, i, e, w, ldp, istar, ze, ng,
                     lcids, pbids, nsamples, exptimes, npb, es, ms, tae):
    npv = k.shape[0]
    npt = t.size
    ldp = atleast_3d(ldp)

    if ldp.shape[0] != npv or ldp.shape[1] != npb:
        raise ValueError(f"The limb darkening profile array should have a shape [npv,npb,ng]")

    t0, p, a, i = atleast_1d(t0), atleast_1d(p), atleast_1d(a), atleast_1d(i)

    flux = zeros((npv, npt))
    for ipv in prange(npv):
        gs, dg, weights = calculate_weights_2d(k[ipv,0], ze, ng)

        ldw = zeros((npb, ng))
        for ipb in range(npb):
            ldw[ipb] = dot(weights, ldp[ipv, ipb])

        for j in range(npt):
            ilc = lcids[j]
            ipb = pbids[ilc]

            if k.shape[1] == 1:
                _k = k[ipv, 0]
            else:
                _k = k[ipv, ipb]

            if isnan(_k) or isnan(a[ipv]) or isnan(i[ipv]):
                flux[ipv, j] = inf
            else:
                for isample in range(1, nsamples[ilc] + 1):
                    time_offset = exptimes[ilc] * ((isample - 0.5) / nsamples[ilc] - 0.5)
                    z = z_ip_s(t[j] + time_offset, t0[ipv], p[ipv], a[ipv], i[ipv], e[ipv], w[ipv], es, ms, tae)
                    if z > 1.0 + _k:
                        flux[ipv, j] += 1.
                    else:
                        iplanet = lerp(z / (1. + _k), dg, ldw[ipb])
                        aplanet = circle_circle_intersection_area(1.0, _k, z)
                        flux[ipv, j] += (istar[ipv, ipb] - iplanet * aplanet) / istar[ipv, ipb]
                flux[ipv, j] /= nsamples[ilc]
    return flux

@njit(parallel=True, fastmath=False)
def swmodel_interpolated_v(t, k, t0, p, a, i, e, w, ldp, istar, weights, dk, k0, dg,
                           lcids, pbids, nsamples, exptimes, npb, es, ms, tae):
    npv = k.shape[0]
    npt = t.size
    ldp = atleast_3d(ldp)
    ng = weights.shape[1]

    if ldp.shape[0] != npv or ldp.shape[1] != npb:
        raise ValueError(f"The limb darkening profile array should have a shape [npv,npb,ng]")

    t0, p, a, i = atleast_1d(t0), atleast_1d(p), atleast_1d(a), atleast_1d(i)

    flux = zeros((npv, npt))
    for ipv in prange(npv):
        ldw = zeros((npb, ng))

        nk = (k[ipv, 0] - k0) / dk
        ik = int(floor(nk))
        ak = nk - ik

        for ipb in range(npb):
            ldw[ipb] = (1.0 - ak) * dot(weights[ik], ldp[ipv, ipb]) + ak * dot(weights[ik + 1], ldp[ipv, ipb])

        for j in range(npt):
            ilc = lcids[j]
            ipb = pbids[ilc]

            if k.shape[1] == 1:
                _k = k[ipv, 0]
            else:
                _k = k[ipv, ipb]

            if isnan(_k) or isnan(a[ipv]) or isnan(i[ipv]):
                flux[ipv, j] = inf
            else:
                for isample in range(1, nsamples[ilc] + 1):
                    time_offset = exptimes[ilc] * ((isample - 0.5) / nsamples[ilc] - 0.5)
                    z = z_ip_s(t[j] + time_offset, t0[ipv], p[ipv], a[ipv], i[ipv], e[ipv], w[ipv], es, ms, tae)
                    if z > 1.0 + _k:
                        flux[ipv, j] += 1.
                    else:
                        iplanet = lerp(z / (1. + _k), dg, ldw[ipb])
                        aplanet = circle_circle_intersection_area(1.0, _k, z)
                        flux[ipv, j] += (istar[ipv, ipb] - iplanet * aplanet) / istar[ipv, ipb]
                flux[ipv, j] /= nsamples[ilc]
    return flux