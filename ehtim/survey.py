# survey.py
# a parameter survey class
#
#    Copyright (C) 2018 Andrew Chael
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import os

import ehtim as eh
import paramsurvey
import paramsurvey.params


##################################################################################################
# ParameterSet object
##################################################################################################

class ParameterSet:

    def __init__(self, paramset, params_fixed={}):

        """An object for one parameter set

           Args:
               paramset (dict): A dict containing single parameter set
               params_fixed (dict): A dict containing non-varying parameters

            Returns:
                a ParameterSet object
        """

        # set each item in paramset dict to an attribute
        for param in paramset:
            setattr(self, param, paramset[param])

        if len(params_fixed) > 0:
            for param in params_fixed:
                setattr(self, param, params_fixed[param])

        self.paramset = paramset
        self.params_fixed = params_fixed
        self.outfile = '%s_%0.7i' % (self.outfile_base, self.i)

        self.fov *= eh.RADPERUAS
        self.reverse_taper_uas *= eh.RADPERUAS
        self.prior_fwhm *= eh.RADPERUAS

    def load_data(self):
        """Loads in uvfits file from self.infile into eht-imaging obs object and averages using self.avg_time
        Creates self.obs

            Args:

            Returns:

        """

        # load the uvfits file
        self.obs = eh.obsdata.load_uvfits(self.infile)

        # identify the scans (times of continuous observation) in the data
        self.obs.add_scans()

        # coherently average
        if self.avg_time == 'scan':
            self.obs = self.obs.avg_coherent(0., scan_avg=True)
        else:
            self.obs = self.obs.avg_coherent(self.avg_time)

    def preimcal(self):

        """
        Applies pre-imaging calibration to self.obs. This includes flagging sites with no measurements, rescaling
        short baselines so only compact flux is being imaged, applying a u-v taper, and adding extra sys noise. Creates
        self.obs_sc which will have self-cal applied in future steps and self.obs_sc_init which is a copy of the initial
        preimcal observation

            Args:

            Returns:

        """

        # handle site name change of APEX between 2017 (AP) to 2018 (AX)
        try:
            self.zbl_tot = np.median(self.obs.unpack_bl('AA', 'AX', 'amp')['amp'])
        except:
            self.zbl_tot = np.median(self.obs.unpack_bl('AA', 'AP', 'amp')['amp'])

        # Flag out sites in the obs.tarr table with no measurements
        allsites = set(self.obs.unpack(['t1'])['t1']) | set(self.obs.unpack(['t2'])['t2'])
        self.obs.tarr = self.obs.tarr[[o in allsites for o in self.obs.tarr['site']]]
        self.obs = eh.obsdata.Obsdata(self.obs.ra, self.obs.dec, self.obs.rf, self.obs.bw, self.obs.data, self.obs.tarr,
                                      source=self.obs.source, mjd=self.obs.mjd,
                                      ampcal=self.obs.ampcal, phasecal=self.obs.phasecal)

        self.obs_orig = self.obs.copy()

        # Rescale short baselines to excise contributions from extended flux.
        # setting zbl < zbl_tot assumes there is an extended constant flux component of zbl_tot-zbl Jy

        if self.zbl != self.zbl_tot:
            for j in range(len(self.obs.data)):
                if (self.obs.data['u'][j] ** 2 + self.obs.data['v'][j] ** 2) ** 0.5 >= self.uv_zblcut: continue
                for field in ['vis', 'qvis', 'uvis', 'vvis', 'sigma', 'qsigma', 'usigma', 'vsigma']:
                    self.obs.data[field][j] *= self.zbl / self.zbl_tot

        self.obs.reorder_tarr_snr()

        self.obs_sc = self.obs.copy()
        # Reverse taper the observation: this enforces a maximum resolution on reconstructed features
        if self.reverse_taper_uas > 0:
            self.obs_sc = self.obs_sc.reverse_taper(self.reverse_taper_uas)

        # Add non-closing systematic noise to the observation
        self.obs_sc = self.obs_sc.add_fractional_noise(self.sys_noise)

        # Make a copy of the initial data (before any self-calibration but after the taper)
        self.obs_sc_init = self.obs_sc.copy()

    def init_img(self):

        """
        Creates initial/prior image. Only gaussian prior option at present, but creates prior attritubute self.initimg
        using self.zbl and self.prior_fwhm

            Args:

            Returns:

        """
        # create guassian prior/inital image
        emptyprior = eh.image.make_square(self.obs_sc, self.npixels, self.fov)

        gaussprior = emptyprior.add_gauss(self.zbl, (self.prior_fwhm, self.prior_fwhm, 0, 0, 0))
        # To avoid gradient singularities in the first step, add an additional small Gaussian
        gaussprior = gaussprior.add_gauss(self.zbl * 1e-3, (self.prior_fwhm, self.prior_fwhm, 0,
                                                            self.prior_fwhm, self.prior_fwhm))
        self.initimg = gaussprior.copy()

    def make_img(self):

        """
        Reconstructs image with specified parameters (data weights, amount of self-cal, etc) described in paramset dict
        Creates attributes self.im_out containing the final image and self.caltab containing a corresponding calibration
        table object for the final image

            Args:

            Returns:

        """
        # specify  data terms
        data_term = {}
        if hasattr(self, 'vis') and self.vis != 0.:
            data_term['vis'] = self.vis
        if hasattr(self, 'amp') and self.amp != 0.:
            data_term['amp'] = self.amp
        if hasattr(self, 'diag_closure') and self.diag_closure is True:
            if hasattr(self, 'logcamp_diag') and self.logcamp_diag != 0.:
                data_term['logcamp_diag'] = self.logcamp
            if hasattr(self, 'cphase_diag') and self.cphase_diag != 0.:
                data_term['cphase_diag'] = self.cphase
        else:
            if hasattr(self, 'logcamp') and self.logcamp != 0.:
                data_term['logcamp'] = self.logcamp
            if hasattr(self, 'cphase') and self.cphase != 0.:
                data_term['cphase'] = self.cphase

        # specify regularizer terms
        reg_term = {}
        if hasattr(self, 'simple') and self.simple != 0.:
            reg_term['simple'] = self.simple
        if hasattr(self, 'tv2') and self.tv2 != 0.:
            reg_term['tv2'] = self.tv2
        if hasattr(self, 'tv') and self.tv != 0.:
            reg_term['tv'] = self.tv
        if hasattr(self, 'l1') and self.l1 != 0.:
            reg_term['l1'] = self.l1
        if hasattr(self, 'flux') and self.flux != 0.:
            reg_term['flux'] = self.flux
        if hasattr(self, 'rgauss') and self.rgauss != 0.:
            reg_term['rgauss'] = self.rgauss

        ### How to make this more general? ###
        # Add systematic noise tolerance for amplitude a-priori calibration errors
        # Start with the SEFD noise (but need sqrt)
        # then rescale to ensure that final results respect the stated error budget
        systematic_noise = self.SEFD_error_budget.copy()
        for key in systematic_noise.keys():
            systematic_noise[key] = ((1.0 + systematic_noise[key]) ** 0.5 - 1.0) * 0.25

        # set up imager
        imgr = eh.imager.Imager(self.obs_sc, self.initimg, prior_im=self.initimg, flux=self.zbl,
                                data_term=data_term, maxit=self.maxit, norm_reg=True, systematic_noise=systematic_noise,
                                reg_term=reg_term, ttype=self.ttype, cp_uv_min=self.uv_zblcut, stop=self.stop)

        res = self.obs.res()

        imgr.make_image_I(show_updates=False, niter=self.niter_static, blur_frac=self.blurfrac)

        if self.selfcal:
            # Self-calibrate to the previous model (phase-only);
            # The solution_interval is 0 to align phases from high and low bands if needed
            self.obs_sc = eh.selfcal(self.obs_sc, imgr.out_last(), method='phase', ttype=self.ttype,
                                     solution_interval=0.0, processes=-1)

            sc_p_idx = 0
            dterms = data_term.keys()
            while sc_p_idx < self.sc_phase:

                # Blur the previous reconstruction to the intrinsic resolution
                init = imgr.out_last().blur_circ(res)

                # Increase the data weights and reinitialize imaging
                if sc_p_idx == 0:
                    for key in dterms:
                        data_term[key] *= self.xdw_phase

                # set up imager
                imgr = eh.imager.Imager(self.obs_sc, init, prior_im=self.initimg, flux=self.zbl,
                                        data_term=data_term, maxit=self.maxit, norm_reg=True,
                                        systematic_noise=systematic_noise,
                                        reg_term=reg_term, ttype=self.ttype, cp_uv_min=self.uv_zblcut, stop=self.stop)

                # Imaging
                imgr.make_image_I(show_updates=False, niter=self.niter_static, blur_frac=self.blurfrac)

                # apply self-calibration to original calibrated data
                self.obs_sc = eh.selfcal(self.obs_sc_init, imgr.out_last(), method='phase', ttype=self.ttype)

                sc_p_idx += 1

            # repeat amp+phase self-calibration
            sc_ap_idx = 0

            while sc_ap_idx < self.sc_ap:

                # Blur the previous reconstruction to the intrinsic resolution
                init = imgr.out_last().blur_circ(res)

                # Increase the data weights and reinitialize imaging
                if sc_p_idx == 0:
                    for key in dterms:
                        data_term[key] *= self.xdw_ap

                # set up imager
                imgr = eh.imager.Imager(self.obs_sc, init, prior_im=self.initimg, flux=self.zbl,
                                        data_term=data_term, maxit=self.maxit, norm_reg=True,
                                        systematic_noise=systematic_noise,
                                        reg_term=reg_term, ttype=self.ttype, cp_uv_min=self.uv_zblcut, stop=self.stop)

                # Imaging
                imgr.make_image_I(show_updates=False, niter=self.niter_static, blur_frac=self.blurfrac)

                caltab = eh.selfcal(self.obs_sc_init, imgr.out_last(), method='both',
                                    ttype=self.ttype, gain_tol=self.gaintol, caltable=True, processes=-1)
                self.obs_sc = caltab.applycal(self.obs_sc_init, interp='nearest', extrapolate=True)

                sc_ap_idx += 1

        self.im_out = imgr.out_last().copy()
        self.caltab = caltab

    def output_results(self):

        """
        Outputs all requested files pertaining to final image

            Args:

            Returns:

        """

        # Add a large gaussian component to account for the missing flux
        # so the final image can be compared with the original data
        self.im_addcmp = self.im_out.add_zblterm(self.obs_orig, self.uv_zblcut, debias=True)
        self.obs_sc_addcmp = eh.selfcal(self.obs_orig, self.im_addcmp, method='both', ttype=self.ttype)

        # If an inverse taper was used, restore the final image
        # to be consistent with the original data
        if self.reverse_taper_uas > 0.0:
            self.im_out = self.im_out.blur_circ(self.reverse_taper_uas)

        # Save the final image
        outfits = os.path.join(self.outpath, '%s.fits' % (self.outfile))
        self.im_out.save_fits(outfits)

        # Save caltab
        if hasattr(self, 'save_caltab') and self.save_caltab == True:
            outcal = os.path.join(self.outpath, '%s/' % (self.outfile))
            eh.caltable.save_caltable(self.caltab, self.obs_sc_init, outcal)

        # Save self-calibrated uvfits
        if self.save_uvfits:
            outuvfits = os.path.join(self.outpath, '%s.uvfits' % (self.outfile))
            self.obs_sc_addcmp.save_uvfits(outuvfits)

        # Save pdf of final image
        if self.save_pdf:
            outpdf = os.path.join(self.outpath, '%s.pdf' % (self.outfile))
            self.im_out.display(cbar_unit=['Tb'], label_type='scale', export_pdf=outpdf)

        # Save pdf of image summary
        if self.save_imgsums:
            # Save an image summary sheet
            plt.close('all')
            outimgsum = os.path.join(self.outpath, '%s_imgsum.pdf' % (self.outfile))
            eh.imgsum(self.im_addcmp, self.obs_sc_addcmp, self.obs_orig, outimgsum, cp_uv_min=self.uv_zblcut,
                      processes=-1)

    def save_statistics(self):

        """
        Saves a csv file with the following statistics:
        chi^2 closure phase, logcamp, vis wrt the original observation
        chi^2 vis wrt to original observation with self-cal to final image applied
        chi^2 closure phase, logcamp, vis wrt the original observation with sys noise and self-cal applied

            Args:

            Returns:

        """
        stats_dict = {}
        stats_dict['i'] = [self.i]

        outstats = os.path.join(self.outpath, '%s_stats.csv' % (self.outfile))

        # if ground truth image available, compute nxcorr
        if self.ground_truth_img != 'None':
            gt_im = eh.image.load_fits(self.ground_truth_img)

            fov_ = 200 * eh.RADPERUAS
            psize_ = fov_ / 256
            nxcorr_, _, _ = gt_im.compare_images(self.im_addcmp, metric='nxcorr', target_fov=fov_, psize=psize_)
            nxcorr = nxcorr_[0]

            stats_dict['nxcorr'] = [nxcorr]

        # chi^2 for closure phase (cp) and log camp (lc)
        # original uv data
        obs_ref = self.obs_orig
        chi2_cp_ref = obs_ref.chisq(self.im_addcmp, dtype='cphase',
                                    ttype=self.ttype, systematic_noise=0.,
                                    systematic_cphase_noise=0, maxset=False,
                                    cp_uv_min=self.uv_zblcut)
        chi2_lc_ref = obs_ref.chisq(self.im_addcmp, dtype='logcamp',
                                    ttype=self.ttype, systematic_noise=0.,
                                    snrcut=1.0, maxset=False,
                                    cp_uv_min=self.uv_zblcut)  # snrcut to remove large chi2 point
        chi2_vis_ref = obs_ref.chisq(self.im_addcmp, dtype='vis',
                                     ttype=self.ttype, systematic_noise=0.,
                                     snrcut=1.0, maxset=False,
                                     cp_uv_min=self.uv_zblcut)  # snrcut to remove large chi2 point

        stats_dict['chi2_cp_ref'] = [chi2_cp_ref]
        stats_dict['chi2_lc_ref'] = [chi2_lc_ref]
        stats_dict['chi2_vis_ref'] = [chi2_vis_ref]

        # orig data self-cal to final image
        obs_sub = self.obs_sc_addcmp
        chi2_vis_sub = obs_sub.chisq(self.im_addcmp, dtype='vis',
                                     ttype=self.ttype, systematic_noise=0.,
                                     snrcut=1.0, maxset=False,
                                     cp_uv_min=self.uv_zblcut)  # snrcut to remove large chi2 point

        stats_dict['chi2_vis_sub'] = [chi2_vis_sub]

        # orig data with sys noise added + self-cal to final image
        self.obs_sc_addcmp_sys = eh.selfcal(self.obs_sc_init, self.im_addcmp, method='both', ttype=self.ttype)
        obs_sys = self.obs_sc_addcmp_sys
        chi2_cp_sys = obs_sys.chisq(self.im_addcmp, dtype='cphase',
                                    ttype=self.ttype, systematic_noise=0.,
                                    systematic_cphase_noise=0, maxset=False,
                                    cp_uv_min=self.uv_zblcut)
        chi2_lc_sys = obs_sys.chisq(self.im_addcmp, dtype='logcamp',
                                    ttype=self.ttype, systematic_noise=0.,
                                    snrcut=1.0, maxset=False,
                                    cp_uv_min=self.uv_zblcut)  # snrcut to remove large chi2 point
        chi2_vis_sys = obs_sys.chisq(self.im_addcmp, dtype='vis',
                                     ttype=self.ttype, systematic_noise=0.,
                                     snrcut=1.0, maxset=False,
                                     cp_uv_min=self.uv_zblcut)  # snrcut to remove large chi2 point

        stats_dict['chi2_cp_sys'] = [chi2_cp_sys]
        stats_dict['chi2_lc_sys'] = [chi2_lc_sys]
        stats_dict['chi2_vis_sys'] = [chi2_vis_sys]

        df = pd.DataFrame.from_dict(stats_dict)
        df.to_csv(outstats)

    def save_params(self):
        """
        Saves a csv file with parameter set details

            Args:

            Returns:

        """
        self.paramset['fov'] = self.fov
        self.paramset['zbl_tot'] = self.zbl_tot

        df = pd.DataFrame.from_dict([self.paramset])

        outparams = os.path.join(self.outpath, '%s_params.csv' % (self.outfile))
        df.to_csv(outparams)

    def run(self):

        """

        Run imaging pipeline for one parameter set.

            Args:

            Returns:

        """

        # if a *_params.csv file exists, it means this parameter has already been run and can be skipped
        # useful in case survey with multiple parameter sets gets interrupted
        outcsv = os.path.join(self.outpath, '%s_params.csv' % (self.outfile))
        if os.path.exists(outcsv):
            pass

        else:

            # load in data
            self.load_data()

            # do pre-imaging calibration
            self.preimcal()

            # create initial/prior image
            self.init_img()

            # run imaging step
            self.make_img()

            # output the results
            self.output_results()

            # save params to text
            self.save_params()

            if self.save_stats:
                self.save_statistics()


def run_pset(pset, system_kwargs, params_fixed):
    """
    Run imaging for one parameter set

           Args:
               pset (dict): A dict containing single parameter set
               params_fixed (dict): A dict containing non-varying parameters

        Returns:

    """
    os.makedirs(params_fixed['outpath'], exist_ok=True)
    PSet = ParameterSet(pset, params_fixed)
    PSet.run()

def run_survey(psets, params_fixed):
    """
    Run survey for all parameter sets using paramsurvey

           Args:
               psets (DataFrame): A pandas DataFrame containing all parameter sets
               params_fixed (dict): A dict containing non-varying parameters

        Returns:

    """
    # run whole survey using map function
    paramsurvey.init(backend=params_fixed['backend'], ncores=params_fixed['nproc'])
    paramsurvey.map(run_pset, psets, user_kwargs=params_fixed, verbose=0)

def create_params_fixed(infile, outfile_base, outpath, ground_truth_img='None',
                        save_imgsums=False, save_uvfits=True, save_pdf=False, save_stats=True, save_caltab=True,
                        nproc=1, backend='multiprocessing', ttype='nfft',
                        selfcal=True, gaintol=[0.02,0.2], niter_static=3, blurfrac=1,
                        maxit=100, stop=1e-4, fov=128, npixels=64, reverse_taper_uas=5, uv_zblcut=0.1e9,
                        SEFD_error_budget={'AA':0.1,'AX':0.1,'GL':0.1,'LM':0.1,'MG':0.1,'MM':0.1,'PV':0.1,'SW':0.1}):

    """
    Create a dict for all non-varying survery parameters

           Args:

                infile (str): path to input uvfits observation file
                outfile_base (str): name of base filename for all outputs
                outpath (str): path to directory where all outputs should be stored
                ground_truth_img (str): if applicable, path to ground truth fits file
                save_imgsums (bool): save summary pdf for each image
                save_uvfits (bool): save final self-cal observation to uvfits file
                save_pdf (bool): save pdf of each image
                save_stats (bool): save csv file containing statistics for each image
                save_caltab (bool): save a calibration table for each image
                nproc (int): number of parallel processes
                backend (str): either 'multiprocessing' or 'ray'
                ttype (str): “fast” or “nfft” or “direct”
                selfcal (bool): perform self-calibration steps during imaging
                gaintol (array): tolerance for gains to be under/over unity respectively during self-cal
                niter_static (int): number of iterations for each imaging step
                blurfrac (int): factor to blur initial image between iterations
                maxit (int): maximum number of iterations if image does not converge
                stop (float): convergence criterion for imaging
                fov (int): image field of view in uas
                npixels (int): number of image pixels
                reverse_taper_uas (int): fwhm of gassuain in uas to reverse taper observation
                uv_zblcut (float): maximum uv-distance to which is considered short baseline flux
                SEFD_error_budget (dict): SEFD percentage error for each station


        Returns:

                params_fixed (dict): dict containing all non-varying survery parameters

    """

    # take all arguments and put them in a dict
    args = list(locals().keys())
    params_fixed = {}
    for arg in args:
        params_fixed[arg] = locals().get(arg)

    return params_fixed

def create_survey_psets(zbl=[0.6], sys_noise=[0.02], avg_time=['scan'], prior_fwhm=[40],
                     sc_phase=[2], xdw_phase=[10], sc_ap=[2], xdw_ap=[1], amp=[0.2], cphase=[1], logcamp=[1],
                     simple=[1], l1=[1], tv=[1], tv2=[1], flux=[1], epsilon_tv=[1e-10]):
    """
    Create a dataframe given all survey parameters. Default values will create an example dataframe but these values
    should be adjusted for each specific observation

           Args:

               zbl (array): compact flux value (Jy)
               sys_noise (array): percent addition of systematic noise
               avg_time (array): in seconds or 'scan' for scan averaging
               prior_fwhm (array): fwhm of gaussian prior in uas
               sc_phase (array): number of iterations to perform phase-only self-cal
               xdw_phase (array): multiplicative factor for data weights after one round of phase-only self-cal
               sc_ap (array): number of iterations to perform amp+phase self-cal
               xdw_ap (array): multiplicative factor for data weights after one round of amp+phase self-cal
               amp (array): data weight to be placed on amplitudes
               cphase (array): data weight to be placed on closure phases
               logcamp (array): data weight to be placed on log closure amplitudes
               simple (array): regularizer weight for relative entropy, favoring similarity to prior image
               l1 (array): regularizer weight for l1 norm, favoring image sparsity
               tv (array): regularizer weight for total variation, favoring sharp edges
               tv2 (array): regularizer weight for total squared variation, favoring smooth edges
               flux (array): regularizer weight for total flux density, favoring final images with flux close to zbl
               epsilon_tv (array): epsilon value used in definition of total variation - rarely need to change this

        Returns:

                psets (DataFrame): pandas DataFrame containing all combination of parameter sets along with index values

    """

    # take all arguments and put them in a dict
    args = list(locals().keys())
    params = {}
    for arg in args:
        params[arg] = locals().get(arg)

    # pandas dataframe containing all combinations of parameters to survey
    psets = paramsurvey.params.product(params)

    # add pset index number to each row of dataframe
    psets['i'] = np.array(range(len(psets)))

    return psets
