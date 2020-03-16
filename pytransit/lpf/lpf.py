#  PyTransit: fast and easy exoplanet transit modelling in Python.
#  Copyright (C) 2010-2019  Hannu Parviainen
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

from pathlib import Path
from typing import List, Union, Iterable

from astropy.stats import sigma_clip
from matplotlib.pyplot import subplots, setp
from numba import njit, prange
from numpy import (inf, sqrt, ones, zeros_like, concatenate, diff, log, ones_like, all,
                   clip, argsort, any, s_, zeros, arccos, nan, full, pi, sum, repeat, asarray, ndarray, log10,
                   array, atleast_2d, isscalar, atleast_1d, where, isfinite, arange, unique, squeeze, ceil, percentile,
                   floor, diag, nanstd)
from numpy.random import uniform, normal, permutation, multivariate_normal
from scipy.stats import norm

try:
    from ldtk import LDPSetCreator
    with_ldtk = True
except ImportError:
    with_ldtk = False

from ..models.transitmodel import TransitModel
from ..orbits.orbits_py import duration_eccentric, as_from_rhop, i_from_ba, i_from_baew, d_from_pkaiews, epoch
from ..param.parameter import ParameterSet, PParameter, GParameter, LParameter
from ..param.parameter import UniformPrior as U, NormalPrior as N, GammaPrior as GM
from .. import QuadraticModel
from .logposteriorfunction import LogPosteriorFunction


@njit(cache=False)
def lnlike_normal(o, m, e):
    return -sum(log(e)) -0.5*o.size*log(2.*pi) - 0.5*sum((o-m)**2/e**2)


@njit(cache=False)
def lnlike_normal_s(o, m, e):
    return -o.size*log(e) -0.5*o.size*log(2.*pi) - 0.5*sum((o-m)**2)/e**2


@njit(parallel=True, cache=False, fastmath=True)
def lnlike_normal_v(o, m, e, wnids, lcids):
    m = atleast_2d(m)
    npv = m.shape[0]
    npt = o.size
    lnl = zeros(npv)
    for i in prange(npv):
        for j in range(npt):
            k = wnids[lcids[j]]
            lnl[i] += -log(e[i,k]) - 0.5*log(2*pi) - 0.5*((o[j]-m[i,j])/e[i,k])**2
    return lnl


@njit(fastmath=True)
def map_pv(pv):
    pv = atleast_2d(pv)
    pvt = zeros((pv.shape[0], 7))
    pvt[:,0]   = sqrt(pv[:,4])
    pvt[:,1:3] = pv[:,0:2]
    pvt[:,  3] = as_from_rhop(pv[:,2], pv[:,1])
    pvt[:,  4] = i_from_ba(pv[:,3], pvt[:,3])
    return pvt


@njit(fastmath=True, cache=False)
def map_ldc(ldc):
    ldc = atleast_2d(ldc)
    uv = zeros_like(ldc)
    a, b = sqrt(ldc[:,0::2]), 2.*ldc[:,1::2]
    uv[:,0::2] = a * b
    uv[:,1::2] = a * (1. - b)
    return uv


class BaseLPF(LogPosteriorFunction):
    _lpf_name = 'BaseLPF'

    def __init__(self, name: str, passbands: list, times: list = None, fluxes: Iterable = None, errors: list = None,
                 pbids: list = None, covariates: list = None, wnids: list = None, tm: TransitModel = None,
                 nsamples: tuple = 1, exptimes: tuple = 0., init_data=True, result_dir: Path = None):
        """The base Log Posterior Function class.

        The `BaseLPF` class creates the basis for transit light curve analyses using `PyTransit`. This class can be
        used in a basic analysis directly, or it can be inherited to create a basis for a more complex analysis.

        Parameters
        ----------
        name: str
            Name of the log posterior function instance.

        passbands: iterable
            List of unique passband names (filters) that the light curves have been observed in.

        times: iterable
            List of 1d ndarrays each containing the mid-observation times for a single light curve.

        fluxes: iterable
            List of 1d ndarrays each containing the normalized fluxes for a single light curve.

        errors: iterable
            List of 1d ndarrays each containing the flux measurement uncertainties for a single light curvel.

        pbids: iterable of ints
            List of passband indices mapping each light curve to a single passband.

        covariates: iterable
            List of covariates one 2d narray per light curve.

        wnids: iterable of ints
            List of noise set indices mapping each light curve to a single noise set.

        tm: TransitModel
            Transitmodel to use instead of the default model.

        nsamples: list[int]
            List of supersampling factors.  The values should be integers and given one per light curve.

        exptimes: list[float]
            List of exposure times. The values should be floats with the time given in days.

        init_data: bool
            Set to `False` to allow the LPF to be initialized without data. This is mainly for debugging.

        result_dir: Path
            Default saving directory
        """

        self._pre_initialisation()

        super().__init__(name=name, result_dir=result_dir)

        self.tm = tm or QuadraticModel(klims=(0.01, 0.75), nk=512, nz=512)

        # Passbands
        # ---------
        # Passbands should be arranged from blue to red
        if isinstance(passbands, (list, tuple, ndarray)):
            self.passbands = passbands
        else:
            self.passbands = [passbands]
        self.npb = npb = len(self.passbands)

        self.nsamples = None
        self.exptimes = None

        # Declare high-level objects
        # --------------------------
        self.ps = None          # Parametrisation
        self.de = None          # Differential evolution optimiser
        self.sampler = None     # MCMC sampler
        self.instrument = None  # Instrument
        self.ldsc = None        # Limb darkening set creator
        self.ldps = None        # Limb darkening profile set
        self.cntm = None        # Contamination model

        # Declare data arrays and variables
        # ---------------------------------
        self.nlc: int = 0                # Number of light curves
        self.n_noise_blocks: int = 0     # Number of noise blocks
        self.noise_ids = None
        self.times: list = None          # List of time arrays
        self.fluxes: list = None         # List of flux arrays
        self.errors: list = None         # List of flux uncertainties
        self.covariates: list = None     # List of covariates
        self.wn: ndarray = None          # Array of white noise estimates for each light curve
        self.timea: ndarray = None       # Array of concatenated times
        self.mfluxa: ndarray = None      # Array of concatenated model fluxes
        self.ofluxa: ndarray = None      # Array of concatenated observed fluxes
        self.errora: ndarray = None      # Array of concatenated model fluxes

        self.lcids: ndarray = None       # Array of light curve indices for each datapoint
        self.pbids: ndarray = None       # Array of passband indices for each light curve
        self.lcslices: list = None       # List of light curve slices

        if init_data:
            # Set up the observation data
            # ---------------------------
            self._init_data(times = times, fluxes = fluxes, pbids = pbids, covariates = covariates,
                            errors = errors, wnids = wnids, nsamples = nsamples, exptimes = exptimes)

            # Set up the parametrisation
            # --------------------------
            self._init_parameters()

            # Inititalise the instrument
            # --------------------------
            self._init_instrument()

        self._post_initialisation()


    def _init_data(self, times: Union[List, ndarray], fluxes: Union[List, ndarray], pbids: Union[List, ndarray] = None,
                   covariates: Union[List, ndarray] = None, errors: Union[List, ndarray] = None, wnids: Union[List, ndarray] = None,
                   nsamples: Union[int, ndarray, Iterable] = 1, exptimes: Union[float, ndarray, Iterable] = 0.):

        if isinstance(times, ndarray) and times.ndim == 1 and times.dtype == float:
            times = [times]

        if isinstance(fluxes, ndarray) and fluxes.ndim == 1 and fluxes.dtype == float:
            fluxes = [fluxes]

        if pbids is None:
            if self.pbids is None:
                self.pbids = zeros(len(fluxes), int)
        else:
            self.pbids = atleast_1d(pbids).astype('int')

        self.nlc = len(times)
        self.times = times
        self.fluxes = fluxes
        self.wn = [nanstd(diff(f)) / sqrt(2) for f in fluxes]
        self.timea = concatenate(self.times)
        self.ofluxa = concatenate(self.fluxes)
        self.mfluxa = zeros_like(self.ofluxa)
        self.lcids = concatenate([full(t.size, i) for i, t in enumerate(self.times)])

        # TODO: Noise IDs get scrambled when removing transits, fix!!!
        if wnids is None:
            if self.noise_ids is None:
                self.noise_ids = zeros(self.nlc, int)
                self.n_noise_blocks = 1
        else:
            self.noise_ids = asarray(wnids)
            self.n_noise_blocks = len(unique(self.noise_ids))
            assert self.noise_ids.size == self.nlc, "Need one noise block id per light curve."
            assert self.noise_ids.max() == self.n_noise_blocks - 1, "Error initialising noise block ids."

        if isscalar(nsamples):
            self.nsamples = full(self.nlc, nsamples)
            self.exptimes = full(self.nlc, exptimes)
        else:
            assert (len(nsamples) == self.nlc) and (len(exptimes) == self.nlc)
            self.nsamples = asarray(nsamples, 'int')
            self.exptimes = asarray(exptimes)

        self.tm.set_data(self.timea, self.lcids, self.pbids, self.nsamples, self.exptimes)

        if errors is None:
            self.errors = array([full(t.size, nan) for t in self.times])
        else:
            self.errors = asarray(errors)
        self.errora = concatenate(self.errors)

        # Initialise the light curves slices
        # ----------------------------------
        self.lcslices = []
        sstart = 0
        for i in range(self.nlc):
            s = self.times[i].size
            self.lcslices.append(s_[sstart:sstart + s])
            sstart += s

        # Initialise the covariate arrays, if given
        # -----------------------------------------
        if covariates is not None:
            self.covariates = covariates
            for cv in self.covariates:
                cv = (cv - cv.mean(0)) / cv.std(0)
            #self.ncovs = self.covariates[0].shape[1]
            #self.covsize = array([c.size for c in self.covariates])
            #self.covstart = concatenate([[0], self.covsize.cumsum()[:-1]])
            #self.cova = concatenate(self.covariates)


    def _init_parameters(self):
        self.ps = ParameterSet()
        self._init_p_orbit()
        self._init_p_planet()
        self._init_p_limb_darkening()
        self._init_p_baseline()
        self._init_p_noise()
        self.ps.freeze()

    def _init_p_orbit(self):
        """Orbit parameter initialisation.
        """
        porbit = [
            GParameter('tc',  'zero_epoch',       'd',      N(0.0,  0.1), (-inf, inf)),
            GParameter('p',   'period',           'd',      N(1.0, 1e-5), (0,    inf)),
            GParameter('rho', 'stellar_density',  'g/cm^3', U(0.1, 25.0), (0,    inf)),
            GParameter('b',   'impact_parameter', 'R_s',    U(0.0,  1.0), (0,      1))]
        self.ps.add_global_block('orbit', porbit)

    def _init_p_planet(self):
        """Planet parameter initialisation.
        """
        pk2 = [PParameter('k2', 'area_ratio', 'A_s', GM(0.1), (0.01**2, 0.75**2))]
        self.ps.add_passband_block('k2', 1, 1, pk2)
        self._pid_k2 = repeat(self.ps.blocks[-1].start, self.npb)
        self._start_k2 = self.ps.blocks[-1].start
        self._sl_k2 = self.ps.blocks[-1].slice

    def _init_p_limb_darkening(self):
        """Limb darkening parameter initialisation.
        """
        pld = concatenate([
            [PParameter(f'q1_{pb}', 'q1 coefficient {pb}', '', U(0, 1), bounds=(0, 1)),
             PParameter(f'q2_{pb}', 'q2 coefficient {pb}', '', U(0, 1), bounds=(0, 1))]
            for i,pb in enumerate(self.passbands)])
        self.ps.add_passband_block('ldc', 2, self.npb, pld)
        self._sl_ld = self.ps.blocks[-1].slice
        self._start_ld = self.ps.blocks[-1].start

    def _init_p_baseline(self):
        """Baseline parameter initialisation.
        """
        self._sl_bl = None

    def _init_p_noise(self):
        """Noise parameter initialisation.
        """
        pns = [LParameter('loge_{:d}'.format(i), 'log10_error_{:d}'.format(i), '', U(-4, 0), bounds=(-4, 0)) for i in range(self.n_noise_blocks)]
        self.ps.add_lightcurve_block('log_err', 1, self.n_noise_blocks, pns)
        self._sl_err = self.ps.blocks[-1].slice
        self._start_err = self.ps.blocks[-1].start

    def _init_instrument(self):
        pass

    def _pre_initialisation(self):
        pass

    def _post_initialisation(self):
        pass

    def create_pv_population(self, npop=50):
        pvp = self.ps.sample_from_prior(npop)
        for sl in self.ps.blocks[1].slices:
            pvp[:,sl] = uniform(0.01**2, 0.25**2, size=(npop, 1))

        # With LDTk
        # ---------
        #
        # Use LDTk to create the sample if LDTk has been initialised.
        #
        if self.ldps:
            istart = self._start_ld
            cms, ces = self.ldps.coeffs_tq()
            for i, (cm, ce) in enumerate(zip(cms.flat, ces.flat)):
                pvp[:, i + istart] = normal(cm, ce, size=pvp.shape[0])

        # No LDTk
        # -------
        #
        # Ensure that the total limb darkening decreases towards
        # red passbands.
        #
        else:
            ldsl = self._sl_ld
            for i in range(pvp.shape[0]):
                pid = argsort(pvp[i, ldsl][::2])[::-1]
                pvp[i, ldsl][::2] = pvp[i, ldsl][::2][pid]
                pvp[i, ldsl][1::2] = pvp[i, ldsl][1::2][pid]

        # Estimate white noise from the data
        # ----------------------------------
        for i in range(self.nlc):
            wn = diff(self.ofluxa).std() / sqrt(2)
            pvp[:, self._start_err] = log10(uniform(0.5*wn, 2*wn, size=npop))
        return pvp

    def add_prior(self, prior):
        self._additional_log_priors.append(prior)

    def baseline(self, pv):
        """Multiplicative baseline"""
        return 1.

    def trends(self, pv):
        """Additive trends"""
        return 0.

    def transit_model(self, pv, copy=True):
        pv = atleast_2d(pv)
        pvp = map_pv(pv)
        ldc = map_ldc(pv[:,self._sl_ld])
        flux = self.tm.evaluate_pv(pvp, ldc, copy)
        return flux

    def flux_model(self, pv):
        baseline    = self.baseline(pv)
        trends      = self.trends(pv)
        model_flux = self.transit_model(pv)
        return baseline * model_flux + trends

    def residuals(self, pv):
        return self.ofluxa - self.flux_model(pv)

    def lnlikelihood(self, pv):
        """Log likelihood for a 1D or 2D array of model parameters.

        Parameters
        ----------
        pv: ndarray
            Either a 1D parameter vector or a 2D parameter array.

        Returns
        -------
            Log likelihood for the given parameter vector(s).
        """
        flux_m = self.flux_model(pv)
        wn = 10**(atleast_2d(pv)[:,self._sl_err])
        return lnlike_normal_v(self.ofluxa, flux_m, wn, self.noise_ids, self.lcids)

    def set_radius_ratio_prior(self, kmin, kmax):
        for p in self.ps[self._sl_k2]:
            p.prior = U(kmin ** 2, kmax ** 2)
            p.bounds = [kmin ** 2, kmax ** 2]
        self.ps.thaw()
        self.ps.freeze()

    def add_t14_prior(self, mean: float, std: float) -> None:
        """Add a normal prior on the transit duration.

        Parameters
        ----------
        mean: float
            Mean of the normal distribution
        std: float
            Standard deviation of the normal distribution.
        """

        def T14(pv):
            pv = atleast_2d(pv)
            a = as_from_rhop(pv[:, 2], pv[:, 1])
            t14 = duration_eccentric(pv[:, 1], sqrt(pv[:, 4]), a, arccos(pv[:, 3] / a), 0, 0, 1)
            return norm.logpdf(t14, mean, std)

        self._additional_log_priors.append(T14)

    def add_as_prior(self, mean: float, std: float) -> None:
        """Add a normal prior on the scaled semi-major axis :math:`(a / R_\star)`.

        Parameters
        ----------
        mean: float
            Mean of the normal distribution.
        std: float
            Standard deviation of the normal distribution
        """
        def as_prior(pv):
            a = as_from_rhop(pv[2], pv[1])
            return norm.logpdf(a, mean, std)
        self._additional_log_priors.append(as_prior)

    def add_ldtk_prior(self, teff: tuple, logg: tuple, z: tuple, passbands: tuple,
                       uncertainty_multiplier: float = 3, **kwargs) -> None:
        """Add a LDTk-based prior on the limb darkening.

        Parameters
        ----------
        teff
        logg
        z
        passbands
        uncertainty_multiplier

        Returns
        -------

        """
        if 'pbs' in kwargs.keys():
            raise DeprecationWarning("The 'pbs' argument has been renamed to 'passbands'")

        if isinstance(passbands[0], str):
            raise DeprecationWarning(
                'Passing passbands by name has been deprecated, they should be now Filter instances.')

        self.ldsc = LDPSetCreator(teff, logg, z, list(passbands))
        self.ldps = self.ldsc.create_profiles(1000)
        self.ldps.resample_linear_z()
        self.ldps.set_uncertainty_multiplier(uncertainty_multiplier)
        def ldprior(pv):
            return self.ldps.lnlike_tq(pv[:, self._sl_ld].reshape([pv.shape[0], -1, 2]))
        self._additional_log_priors.append(ldprior)

    def remove_outliers(self, sigma=5):
        fmodel = squeeze(self.flux_model(self.de.minimum_location))
        covariates = [] if self.covariates is not None else None
        times, fluxes, lcids, errors = [], [], [], []
        for i in range(len(self.times)):
            res = self.fluxes[i] - fmodel[self.lcslices[i]]
            mask = ~sigma_clip(res, sigma=sigma).mask
            times.append(self.times[i][mask])
            fluxes.append(self.fluxes[i][mask])
            if covariates is not None:
                covariates.append(self.covariates[i][mask])
            if self.errors is not None:
                errors.append(self.errors[i][mask])

        self._init_data(times=times, fluxes=fluxes, covariates=self.covariates, pbids=self.pbids,
                        errors=(errors if self.errors is not None else None), wnids=self.noise_ids,
                        nsamples=self.nsamples, exptimes=self.exptimes)

    def remove_transits(self, tids):
        m = ones(len(self.times), bool)
        m[tids] = False
        self._init_data(self.times[m], self.fluxes[m], self.pbids[m],
                        self.covariates[m] if self.covariates is not None else None,
                        self.errors[m], self.noise_ids[m], self.nsamples[m], self.exptimes[m])
        self._init_parameters()

    def posterior_samples(self, burn: int = 0, thin: int = 1, derived_parameters: bool = True):
        df = super().posterior_samples(burn=burn, thin=thin)
        if derived_parameters:
            for k2c in df.columns[self._sl_k2]:
                df[k2c.replace('k2', 'k')] = sqrt(df[k2c])
            df['a'] = as_from_rhop(df.rho.values, df.p.values)
            df['inc'] = i_from_baew(df.b.values, df.a.values, 0., 0.)

            average_ks = sqrt(df.iloc[:, self._sl_k2]).mean(1).values
            df['t14'] = d_from_pkaiews(df.p.values, average_ks, df.a.values, df.inc.values, 0., 0., 1)
        return df

    def plot_light_curves(self, method='de', ncol: int = 3, width: float = 2., max_samples: int = 1000, figsize=None,
                          data_alpha=0.5, ylim=None):
        nrow = int(ceil(self.nlc / ncol))
        if method == 'mcmc':
            df = self.posterior_samples(derived_parameters=False)
            t0, p = df.tc.median(), df.p.median()
            fmodel = self.flux_model(permutation(df.values)[:max_samples])
            fmperc = percentile(fmodel, [50, 16, 84, 2.5, 97.5], 0)
        else:
            fmodel = squeeze(self.flux_model(self.de.minimum_location))
            t0, p = self.de.minimum_location[0], self.de.minimum_location[1]
            fmperc = None

        fig, axs = subplots(nrow, ncol, figsize=figsize, constrained_layout=True, sharey='all', sharex='all',
                            squeeze=False)
        for i in range(self.nlc):
            ax = axs.flat[i]
            e = epoch(self.times[i].mean(), t0, p)
            tc = t0 + e * p
            time = self.times[i] - tc

            ax.plot(time, self.fluxes[i], '.', alpha=data_alpha)

            if method == 'de':
                ax.plot(time, fmodel[self.lcslices[i]], 'w', lw=4)
                ax.plot(time, fmodel[self.lcslices[i]], 'k', lw=1)
            else:
                ax.fill_between(time, *fmperc[3:5, self.lcslices[i]], alpha=0.15)
                ax.fill_between(time, *fmperc[1:3, self.lcslices[i]], alpha=0.25)
                ax.plot(time, fmperc[0, self.lcslices[i]])

            setp(ax, xlabel=f'Time - T$_c$ [d]', xlim=(-width / 2 / 24, width / 2 / 24))
        setp(axs[:, 0], ylabel='Normalised flux')

        if ylim is not None:
            setp(axs, ylim=ylim)

        for ax in axs.flat[self.nlc:]:
            ax.remove()
        return fig

    def __repr__(self):
        return f"Target: {self.name}\nLPF: {self._lpf_name}\n Passbands: {self.passbands}"
