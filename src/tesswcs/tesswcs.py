"""Work with WCS objects for TESS"""
import bz2
import json

import astropy.units as u
import numpy as np
from astropy.coordinates import Angle, SkyCoord
from astropy.io import fits
from astropy.wcs import WCS as astropyWCS
from astropy.wcs import Sip
from tqdm import tqdm

from . import (
    PACKAGEDIR,
    Ms,
    pixel_corners,
    rcolumns,
    rrows,
    sip_dict,
    wcs_dicts,
    xcent,
    xs,
    ycent,
    ys,
)
from .utils import angle_to_matrix, get_M

# from . import pointings  # noqa: E402


class WCS(astropyWCS):
    """A special subclass of astropy.wcs.WCS

    This class allows us to add attributes to the WCS header and add class methods
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @classmethod
    def from_archive(cls, sector: int, camera: int, ccd: int):
        """Load a WCS from archival TESS data"""
        wcs = cls(naxis=2)
        wcs.sector, wcs.camera, wcs.ccd = sector, camera, ccd
        wcs.ra = wcs_dicts[sector]["ra"]
        wcs.dec = wcs_dicts[sector]["dec"]
        wcs.roll = wcs_dicts[sector]["roll"]
        wcs.pixel_shape = (rrows, rcolumns)
        wcs.wcs.ctype = ["RA---TAN-SIP", "DEC--TAN-SIP"]
        wcs.wcs.cunit = ["deg", "deg"]
        wcs.wcs.radesys = "ICRS"
        wcs.wcs.crpix = wcs_dicts[sector][camera][ccd]["crpix0"]
        wcs.wcs.crval = wcs_dicts[sector][camera][ccd]["crval0"]
        wcs.wcs.cdelt = [1, 1]
        wcs.wcs.pc = wcs_dicts[sector][camera][ccd]["cd"]
        wcs.sip = Sip(
            *[sip_dict[attr][camera][ccd] for attr in ["a", "b", "ap", "bp"]],
            [1045, 1001],
        )
        return wcs

    @classmethod
    def predict(
        cls,
        ra: float,
        dec: float,
        roll: float,
        camera: int,
        ccd: int,
        warp: bool = True,
    ):
        """Predict a WCS based on RA, Dec and roll


        warp : bool
            Whether to warp by the best fit TESS Camera/CCD warp. This is recommended,
            otherwise solution will be off by up to 10 pixels. This must be switched off
            if fitting for the warp.
        """
        if (ccd == 1) | (ccd == 3):
            C = np.asarray([[1, -1], [-1, 1]])
        elif (ccd == 2) | (ccd == 4):
            C = np.asarray([[1, 1], [1, 1]])

        c_boresight = SkyCoord(ra, dec, unit="deg")
        R = angle_to_matrix(-roll)

        # initialize object
        wcs = cls(naxis=2)
        wcs.ra, wcs.dec, wcs.roll, wcs.camera, wcs.ccd = ra, dec, roll, camera, ccd
        wcs.pixel_shape = (rrows, rcolumns)

        # center of the CCD
        xc, yc = xcent[camera][ccd], ycent[camera][ccd]
        r, phi = np.hypot(xc, yc) * u.deg, np.arctan2(yc, xc) * u.rad
        c_ccd = c_boresight.directional_offset_by(
            position_angle=phi - roll * u.deg, separation=r
        )

        wcs.wcs.ctype = ["RA---TAN-SIP", "DEC--TAN-SIP"]
        wcs.wcs.cunit = ["deg", "deg"]
        wcs.wcs.radesys = "ICRS"
        #        wcs.wcs.crpix = np.asarray([rrows / 2, rcolumns / 2])
        wcs.wcs.crpix = np.asarray([1045, 1001])
        wcs.wcs.cdelt = [-1, 1]
        # center is the ccd center in world space
        wcs.wcs.crval = [c_ccd.ra.deg, c_ccd.dec.deg]
        wcs.sip = Sip(
            *[sip_dict[attr][camera][ccd] for attr in ["a", "b", "ap", "bp"]],
            [1045, 1001],
        )
        # rotate the corners of the CCD to the correct roll
        x, y = R.dot(np.vstack([xs[camera][ccd], ys[camera][ccd]]))
        r, phi = np.hypot(x, y) * u.deg, np.arctan2(y, x) * u.rad

        # positions of the corners in world space
        c = c_boresight.directional_offset_by(position_angle=phi, separation=r)
        r, phi = c_ccd.separation(c), c_ccd.position_angle(c)

        # position of the corners in detector space
        truth = np.asarray([r * np.cos(phi), r * np.sin(phi)])

        # Our estimate of where the corners should in world space
        c = wcs.pixel_to_world(*pixel_corners.T)
        r, phi = c_ccd.separation(c), c_ccd.position_angle(c)
        # Our estimate of where the corners should in detector space
        approx = np.asarray([r * np.cos(phi), r * np.sin(phi)])

        # fit a transformation matrix
        matrix = get_M(truth.T, approx.T)[:2, :2]
        wcs.wcs.pc = matrix.dot(angle_to_matrix(180)).dot(
            np.asarray([[0.0593726, 0.00123252], [0.00123252, 0.0593726]]) * (C)
        )
        if warp:
            wcs.wcs.pc = wcs.wcs.pc.dot(Ms[camera][ccd][:2, :2])
            wcs.wcs.crpix += Ms[camera][ccd][:2, 2]
        return wcs

    def to_header(self, key=None):
        hdr = super().to_header(relax=True, key=key)
        self._update_header(hdr)
        return hdr

    def to_fits(self, **kwargs):
        hdu = fits.PrimaryHDU(header=self.to_header())
        hdulist = fits.HDUList([hdu])
        return hdulist

    def _update_header(self, hdr):
        hdr["AUTHOR"] = "tesswcs"
        for attr in ["ra", "dec", "roll", "sector", "camera", "ccd"]:
            if hasattr(self, attr):
                hdr[attr] = getattr(self, attr)
        return hdr


def _build_warp_matrices():
    R, C = np.meshgrid(
        np.arange(0, rrows, 10), np.arange(0, rcolumns, 10), indexing="ij"
    )

    Ms = {
        1: {1: [], 2: [], 3: [], 4: []},
        2: {1: [], 2: [], 3: [], 4: []},
        3: {1: [], 2: [], 3: [], 4: []},
        4: {1: [], 2: [], 3: [], 4: []},
    }

    for sector in tqdm(np.arange(1, 14)):
        # fig, ax = plt.subplots(4, 4, figsize=(10, 10))
        for camera in np.arange(1, 5):
            for ccd in np.arange(1, 5):
                wcs_t = WCS.from_archive(sector=sector, camera=camera, ccd=ccd)
                truth = wcs_t.all_pix2world(np.asarray([R.ravel(), C.ravel()]).T, 0)

                wcs_c = WCS.predict(
                    ra=wcs_t.ra,
                    dec=wcs_t.dec,
                    roll=wcs_t.roll,
                    camera=wcs_t.camera,
                    ccd=wcs_t.ccd,
                )
                prediction = wcs_c.all_pix2world(
                    np.asarray([R.ravel(), C.ravel()]).T, 0
                )

                if (wcs_t.wcs.crval[0] > 90) & (wcs_t.wcs.crval[0] < 270):
                    wrap = 360 * u.deg
                else:
                    wrap = 180 * u.deg
                truth = np.asarray(
                    [Angle(truth[:, 0] * u.deg).wrap_at(wrap).deg, truth[:, 1]]
                ).T
                prediction = np.asarray(
                    [
                        Angle(prediction[:, 0] * u.deg).wrap_at(wrap).deg,
                        prediction[:, 1],
                    ]
                ).T

                M = get_M(truth, prediction)
                transformation = M.dot(
                    np.asarray(
                        [prediction[:, 0], prediction[:, 1], prediction[:, 0] ** 0]
                    )
                )[:2].T
                pix = wcs_c.wcs_world2pix(transformation, 0)
                M2 = np.linalg.inv(get_M(np.asarray([R.ravel(), C.ravel()]).T, pix))

                wcs_c.wcs.pc = wcs_c.wcs.pc.dot(M2[:2, :2])
                prediction = wcs_c.wcs_pix2world(
                    np.asarray([R.ravel(), C.ravel()]).T, 0
                )
                prediction = np.asarray(
                    [
                        Angle(prediction[:, 0] * u.deg).wrap_at(wrap).deg,
                        prediction[:, 1],
                    ]
                ).T
                M2[:2, 2] = (
                    wcs_c.wcs_world2pix([prediction.mean(axis=0)], 0)[0]
                    - wcs_c.wcs_world2pix([truth.mean(axis=0)], 0)[0]
                )

                wcs_c.wcs.crpix += M2[:2, 2]

                prediction = wcs_c.wcs_pix2world(
                    np.asarray([R.ravel(), C.ravel()]).T, 0
                )
                prediction = np.asarray(
                    [
                        Angle(prediction[:, 0] * u.deg).wrap_at(wrap).deg,
                        prediction[:, 1],
                    ]
                ).T

                # sep = np.hypot(*(truth - prediction).T).reshape(R.shape)
                # ax[camera-1][ccd-1].pcolormesh(R, C, sep, vmin=0, vmax=3*21/3600)
                Ms[camera][ccd].append(M2.copy())

    for camera in np.arange(1, 5):
        for ccd in np.arange(1, 5):
            Ms[int(camera)][int(ccd)] = np.median(Ms[camera][ccd], axis=0).tolist()

    filename = f"{PACKAGEDIR}/data/TESS_wcs_Ms.json.bz2"
    json_data = json.dumps(Ms)
    with bz2.open(filename, "wt", encoding="utf-8") as f:
        f.write(json_data)
