# -*- coding: utf-8 -*-
# Copyright 2017-2019 The pyXem developers
#
# This file is part of pyXem.
#
# pyXem is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# pyXem is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with pyXem.  If not, see <http://www.gnu.org/licenses/>.

import numpy as np

from hyperspy.signals import BaseSignal, Signal1D
from hyperspy.api import markers

import matplotlib.pyplot as plt
from matplotlib.cm import get_cmap
from scipy.spatial import distance_matrix
from sklearn.cluster import DBSCAN

from warnings import warn

from pyxem.signals import push_metadata_through
from pyxem.signals import transfer_navigation_axes
from pyxem.utils.vector_utils import detector_to_fourier
from pyxem.utils.vector_utils import calculate_norms, calculate_norms_ragged
from pyxem.utils.vector_utils import get_npeaks, filter_vectors_ragged
from pyxem.utils.vector_utils import filter_vectors_edge_ragged
from pyxem.utils.expt_utils import peaks_as_gvectors
from pyxem.utils.plot import generate_marker_inputs_from_peaks


"""
Signal class for diffraction vectors.

There are two cases that are supported:

1. A map of diffraction vectors, which will in general be a ragged signal of
signals. It the navigation dimensions of the map and contains a signal for each
peak at every position.

2. A list of diffraction vectors with dimensions < n | 2 > where n is the
number of peaks.
"""


class DiffractionVectors(BaseSignal):
    """Crystallographic mapping results containing the best matching crystal
    phase and orientation at each navigation position with associated metrics.

    Attributes
    ----------
    cartesian : np.array()
        Array of 3-vectors describing Cartesian coordinates associated with
        each diffraction vector.
    hkls : np.array()
        Array of Miller indices associated with each diffraction vector
        following indexation.
    """
    _signal_type = "diffraction_vectors"

    def __init__(self, *args, **kwargs):
        self, args, kwargs = push_metadata_through(self, *args, **kwargs)
        super().__init__(*args, **kwargs)
        self.cartesian = None
        self.hkls = None
        self.detector_shape = None
        self.pixel_calibration = None

    @classmethod
    def from_peaks(cls, peaks, center, calibration):
        """Takes a list of peak positions (pixel coordinates) and returns
        an instance of `Diffraction2D`

        Parameters
        ----------
        peaks : Signal
            Signal containing lists (np.array) of pixel coordinates specifying
            the reflection positions
        center : np.array
            Diffraction pattern center in array indices.
        calibration : np.array
            Calibration in reciprocal Angstroms per pixels for each of the dimensions.

        Returns
        -------
        vectors : :obj:`pyxem.signals.diffraction_vectors.DiffractionVectors`
            List of diffraction vectors
        """
        gvectors = peaks.map(peaks_as_gvectors,
                             center=center,
                             calibration=calibration,
                             inplace=False)

        vectors = cls(gvectors)
        vectors.axes_manager.set_signal_dimension(0)

        return vectors

    def plot_diffraction_vectors(self, xlim=1.0, ylim=1.0,
                                 unique_vectors=None,
                                 distance_threshold=0.01,
                                 method='distance_comparison',
                                 min_samples=1,
                                 image_to_plot_on=None,
                                 image_cmap='gray',
                                 plot_label_colors=False,
                                 distance_threshold_all=0.005):  # pragma: no cover
        """Plot the unique diffraction vectors.

        Parameters
        ----------
        xlim : float
            The maximum x coordinate to be plotted.
        ylim : float
            The maximum y coordinate in reciprocal Angstroms to be plotted.
        unique_vectors : DiffractionVectors, optional
            The unique vectors to be plotted (optional). If not given, the
            unique vectors will be found by get_unique_vectors.
        distance_threshold : float, optional
            The minimum distance in reciprocal Angstroms between diffraction
            vectors for them to be considered unique diffraction vectors.
            Will be passed to get_unique_vectors if no unique vectors are
            given.
        method : str
            The method to use to determine unique vectors, if not given.
            Valid methods are 'strict', 'distance_comparison' and 'DBSCAN'.
            'strict' returns all vectors that are strictly unique and
            corresponds to distance_threshold=0.
            'distance_comparison' checks the distance between vectors to
            determine if some should belong to the same unique vector,
            and if so, the unique vector is iteratively updated to the
            average value.
            'DBSCAN' relies on the DBSCAN [1] clustering algorithm, and
            uses the Eucledian distance metric.
        min_samples : int, optional
            The minimum number of not identical vectors within one cluster
            for it to be considered a core sample, i.e. to not be considered
            noise. Will be passed to get_unique_vectors if no unique vectors
            are given. Only used if method=='DBSCAN'.
        image_to_plot_on : BaseSignal, optional
            If provided, the vectors will be plotted on top of this image.
            The image must be calibrated in terms of offset and scale.
        image_cmap : str, optional
            The colormap to plot the image in.
        plot_label_colors : bool, optional
            If True (default is False), also the vectors contained within each
            cluster will be plotted, with colors according to their
            cluster membership. If True, the unique vectors will be
            calculated by get_unique_vectors. Requires on method=='DBSCAN'.
        distance_threshold_all : float, optional
            The minimum distance, in calibrated units, between diffraction
            vectors inside one cluster for them to be plotted. Only used if
            plot_label_colors is True and requires method=='DBSCAN'.

        Returns
        -------
        fig : matplotlib figure
            The plot as a matplotlib figure.

        """
        fig = plt.figure()
        ax = fig.add_subplot(111)
        offset, scale = 0., 1.
        if image_to_plot_on is not None:
            offset = image_to_plot_on.axes_manager[-1].offset
            scale = image_to_plot_on.axes_manager[-1].scale
            ax.imshow(image_to_plot_on, cmap=image_cmap)
        else:
            ax.set_xlim(-xlim, xlim)
            ax.set_ylim(ylim, -ylim)
            ax.set_aspect('equal')

        if plot_label_colors is True and method == 'DBSCAN':
            clusters = self.get_unique_vectors(
                distance_threshold, method='DBSCAN', min_samples=min_samples,
                return_clusters=True)[1]
            labs = clusters.labels_[clusters.core_sample_indices_]
            # Get all vectors from the clustering not considered noise
            cores = clusters.components_
            if cores.size == 0:
                warn('No clusters were found. Check parameters, or '
                     'use plot_label_colors=False.')
            else:
                peaks = DiffractionVectors(cores)
                peaks.axes_manager.set_signal_dimension(1)
                # Since this original number of vectors can be huge, we
                # find a reduced number of vectors that should be plotted, by
                # running a new clustering on all the vectors not considered
                # noise, considering distance_threshold_all.
                peaks = peaks.get_unique_vectors(
                    distance_threshold_all, min_samples=1,
                    return_clusters=False)
                peaks_all_len = peaks.data.shape[0]
                labels_to_plot = np.zeros(peaks_all_len)
                peaks_to_plot = np.zeros((peaks_all_len, 2))
                # Find the labels of each of the peaks to plot by referring back
                # to the list of labels for the original vectors.
                for n, peak in zip(np.arange(peaks_all_len), peaks):
                    index = distance_matrix([peak.data], cores).argmin()
                    peaks_to_plot[n] = cores[index]
                    labels_to_plot[n] = labs[index]
                # Assign a color value to each label, and shuffle these so that
                # adjacent clusters hopefully get distinct colors.
                cmap_lab = get_cmap('gist_rainbow')
                lab_values_shuffled = np.arange(np.max(labels_to_plot) + 1)
                np.random.shuffle(lab_values_shuffled)
                labels_steps = np.array(list(map(
                    lambda n: lab_values_shuffled[int(n)], labels_to_plot)))
                labels_steps = labels_steps / (np.max(labels_to_plot) + 1)
                # Plot all peaks
                for lab, peak in zip(labels_steps, peaks_to_plot):
                    ax.plot((peak[0] - offset) / scale,
                            (peak[1] - offset) / scale, '.',
                            color=cmap_lab(lab))
        if unique_vectors is None:
            unique_vectors = self.get_unique_vectors(
                distance_threshold, method=method, min_samples=min_samples)
        # Plot the unique vectors
        ax.plot((unique_vectors.data.T[0] - offset) / scale,
                (unique_vectors.data.T[1] - offset) / scale, 'kx')
        plt.tight_layout()
        plt.axis('off')
        return fig

    def plot_diffraction_vectors_on_signal(self, signal, *args, **kwargs):
        """Plot the diffraction vectors on a signal.

        Parameters
        ----------
        signal : ElectronDiffraction2D
            The ElectronDiffraction2D signal object on which to plot the peaks.
            This signal must have the same navigation dimensions as the peaks.
        *args :
            Arguments passed to signal.plot()
        **kwargs :
            Keyword arguments passed to signal.plot()
        """
        mmx, mmy = generate_marker_inputs_from_peaks(self)
        signal.plot(*args, **kwargs)
        for mx, my in zip(mmx, mmy):
            m = markers.point(x=mx, y=my, color='red', marker='x')
            signal.add_marker(m, plot_marker=True, permanent=False)

    def get_magnitudes(self, *args, **kwargs):
        """Calculate the magnitude of diffraction vectors.

        Parameters
        ----------
        *args:
            Arguments to be passed to map().
        **kwargs:
            Keyword arguments to map().

        Returns
        -------
        magnitudes : BaseSignal
            A signal with navigation dimensions as the original diffraction
            vectors containging an array of gvector magnitudes at each
            navigation position.

        """
        # If ragged the signal axes will not be defined
        if len(self.axes_manager.signal_axes) == 0:
            magnitudes = self.map(calculate_norms_ragged,
                                  inplace=False,
                                  *args, **kwargs)
        # Otherwise easier to calculate.
        else:
            magnitudes = BaseSignal(calculate_norms(self))
            magnitudes.axes_manager.set_signal_dimension(0)

        return magnitudes

    def get_magnitude_histogram(self, bins, *args, **kwargs):
        """Obtain a histogram of gvector magnitudes.

        Parameters
        ----------
        bins : numpy array
            The bins to be used to generate the histogram.
        *args:
            Arguments to get_magnitudes().
        **kwargs:
            Keyword arguments to get_magnitudes().

        Returns
        -------
        ghis : Signal1D
            Histogram of gvector magnitudes.

        """
        gmags = self.get_magnitudes(*args, **kwargs)

        if len(self.axes_manager.signal_axes) == 0:
            glist = []
            for i in gmags._iterate_signal():
                for j in np.arange(len(i[0])):
                    glist.append(i[0][j])
            gs = np.asarray(glist)
            gsig = Signal1D(gs)
            ghis = gsig.get_histogram(bins=bins)

        else:
            ghis = gmags.get_histogram(bins=bins)

        ghis.axes_manager.signal_axes[0].name = 'k'
        ghis.axes_manager.signal_axes[0].units = '$A^{-1}$'

        return ghis

    def get_unique_vectors(self, distance_threshold=0.01,
                           method='distance_comparison', min_samples=1,
                           return_clusters=False):
        """Returns diffraction vectors considered unique by:
        strict comparison, distance comparison with a specified
        threshold, or by clustering using DBSCAN [1].

        Parameters
        ----------
        distance_threshold : float
            The minimum distance between diffraction vectors for them to
            be considered unique diffraction vectors. If
            distance_threshold==0, the unique vectors will be determined
            by strict comparison.
        method : str
            The method to use to determine unique vectors. Valid methods
            are 'strict', 'distance_comparison' and 'DBSCAN'.
            'strict' returns all vectors that are strictly unique and
            corresponds to distance_threshold=0.
            'distance_comparison' checks the distance between vectors to
            determine if some should belong to the same unique vector,
            and if so, the unique vector is iteratively updated to the
            average value.
            'DBSCAN' relies on the DBSCAN [1] clustering algorithm, and
            uses the Eucledian distance metric.
        min_samples : int, optional
            The minimum number of not strictly identical vectors within
            one cluster for the cluster to be considered a core sample,
            i.e. to not be considered noise. Only used for method='DBSCAN'.
        return_clusters : bool, optional
            If True (False is default), the DBSCAN clustering result is
            returned. Only used for method='DBSCAN'.

        References
        ----------
        [1] https://scikit-learn.org/stable/modules/generated/sklearn.
            cluster.DBSCAN.html

        Returns
        -------
        unique_peaks : DiffractionVectors
            The unique diffraction vectors.
        clusters : DBSCAN
            The results from the clustering, given as class DBSCAN.
            Only returned if method='DBSCAN' and return_clusters=True.
        """
        # Flatten the array of peaks to reach dimension (n, 2), where n
        # is the number of peaks.
        peaks_all = np.concatenate([
            peaks.ravel() for peaks in self.data.flat]).reshape(-1, 2)

        # A distance_threshold of 0 implies a strict comparison. So in that
        # case, a warning is raised unless the specified method is 'strict'.
        if distance_threshold == 0:
            if method is not 'strict':
                warn(message='distance_threshold=0 was given, and therefore ' +
                     'a strict comparison is used, even though the ' +
                     'specified method was ' + method + '.')
                method = 'strict'

        if method == 'strict':
            unique_peaks = np.unique(peaks_all, axis=0)

        elif method == 'distance_comparison':
            unique_vectors, unique_counts = np.unique(
                peaks_all, axis=0, return_counts=True)

            unique_peaks = np.array([[0, 0]])
            unique_peaks_counts = np.array([0])

            while unique_vectors.shape[0] > 0:
                unique_vector = unique_vectors[0]
                distances = distance_matrix(
                    np.array([unique_vector]), unique_vectors)
                indices = np.where(distances < distance_threshold)[1]

                new_count = indices.size
                new_unique_peak = np.array([np.average(
                    unique_vectors[indices], weights=unique_counts[indices],
                    axis=0)])

                unique_peaks = np.append(unique_peaks, new_unique_peak,
                                         axis=0)

                unique_peaks_counts = np.append(unique_peaks_counts,
                                                new_count)
                unique_vectors = np.delete(unique_vectors, indices, axis=0)
                unique_counts = np.delete(unique_counts, indices, axis=0)
            unique_peaks = np.delete(unique_peaks, [0], axis=0)

        elif method == 'DBSCAN':
            # All peaks are clustered by DBSCAN so that peaks within
            # one cluster are separated by distance_threshold or less.
            unique_vectors, unique_vectors_counts = np.unique(
                peaks_all, axis=0, return_counts=True)
            clusters = DBSCAN(
                eps=distance_threshold, min_samples=min_samples,
                metric='euclidean').fit(
                unique_vectors, sample_weight=unique_vectors_counts)
            unique_labels, unique_labels_count = np.unique(
                clusters.labels_, return_counts=True)
            unique_peaks = np.zeros((unique_labels.max() + 1, 2))

            # For each cluster, a center of mass is calculated based
            # on all the peaks within the cluster, and the center of
            # mass is taken as the final unique vector position.
            for n in np.arange(unique_labels.max() + 1):
                peaks_n_temp = unique_vectors[clusters.labels_ == n]
                peaks_n_counts_temp = unique_vectors_counts[
                    clusters.labels_ == n]
                unique_peaks[n] = np.average(
                    peaks_n_temp, weights=peaks_n_counts_temp, axis=0)

        # Manipulate into DiffractionVectors class
        if unique_peaks.size > 0:
            unique_peaks = DiffractionVectors(unique_peaks)
            unique_peaks.axes_manager.set_signal_dimension(1)
        if return_clusters and method == 'DBSCAN':
            return unique_peaks, clusters
        else:
            return unique_peaks

    def filter_vectors_magnitudes(self, min_magnitude, max_magnitude,
                                  *args, **kwargs):
        """Filter the diffraction vectors to accept only those with magnitudes
        within a user specified range.

        Parameters
        ----------
        min_magnitude : float
            Minimum allowed vector magnitude.
        max_magnitude : float
            Maximum allowed vector magnitude.
        *args:
            Arguments to be passed to map().
        **kwargs:
            Keyword arguments to map().

        Returns
        -------
        filtered_vectors : DiffractionVectors
            Diffraction vectors within allowed magnitude tolerances.
        """
        # If ragged the signal axes will not be defined
        if len(self.axes_manager.signal_axes) == 0:
            filtered_vectors = self.map(filter_vectors_ragged,
                                        min_magnitude=min_magnitude,
                                        max_magnitude=max_magnitude,
                                        inplace=False,
                                        *args, **kwargs)
            # Type assignment to DiffractionVectors for return
            filtered_vectors = DiffractionVectors(filtered_vectors)
            filtered_vectors.axes_manager.set_signal_dimension(0)
        # Otherwise easier to calculate.
        else:
            magnitudes = self.get_magnitudes()
            magnitudes.data[magnitudes.data < min_magnitude] = 0
            magnitudes.data[magnitudes.data > max_magnitude] = 0
            filtered_vectors = self.data[np.where(magnitudes)]
            # Type assignment to DiffractionVectors for return
            filtered_vectors = DiffractionVectors(filtered_vectors)
            filtered_vectors.axes_manager.set_signal_dimension(1)

        transfer_navigation_axes(filtered_vectors, self)

        return filtered_vectors

    def filter_vectors_detector_edge(self, exclude_width,
                                     *args, **kwargs):
        """Filter the diffraction vectors to accept only those not within a
        user specified proximity to the detector edge.

        Parameters
        ----------
        exclude_width : int
            The width of the region adjacent to the detector edge from which
            vectors will be excluded.
        *args:
            Arguments to be passed to map().
        **kwargs:
            Keyword arguments to map().

        Returns
        -------
        filtered_vectors : DiffractionVectors
            Diffraction vectors within allowed detector region.
        """
        x_threshold = self.pixel_calibration * (self.detector_shape[0] / 2) - self.pixel_calibration * exclude_width
        y_threshold = self.pixel_calibration * (self.detector_shape[1] / 2) - self.pixel_calibration * exclude_width
        # If ragged the signal axes will not be defined
        if len(self.axes_manager.signal_axes) == 0:
            filtered_vectors = self.map(filter_vectors_edge_ragged,
                                        x_threshold=x_threshold,
                                        y_threshold=y_threshold,
                                        inplace=False,
                                        *args, **kwargs)
            # Type assignment to DiffractionVectors for return
            filtered_vectors = DiffractionVectors(filtered_vectors)
            filtered_vectors.axes_manager.set_signal_dimension(0)
        # Otherwise easier to calculate.
        else:
            tmp_data = self.data.copy()
            tmp_data[np.absolute(tmp_data.T[0]) > x_threshold] = 0
            tmp_data[np.absolute(tmp_data.T[1]) > y_threshold] = 0
            filtered_vectors = self.data[np.where(tmp_data.T[0])]
            # Type assignment to DiffractionVectors for return
            filtered_vectors = DiffractionVectors(filtered_vectors)
            filtered_vectors.axes_manager.set_signal_dimension(1)

        transfer_navigation_axes(filtered_vectors, self)

        return filtered_vectors

    def get_diffracting_pixels_map(self, binary=False):
        """Map of the number of vectors at each navigation position.

        Parameters
        ----------
        binary : boolean
            If True a binary image with diffracting pixels taking value == 1 is
            returned.

        Returns
        -------
        crystim : Signal2D
            2D map of diffracting pixels.
        """
        crystim = self.map(get_npeaks, inplace=False).as_signal2D((0, 1))

        if binary is True:
            crystim = crystim == 1

        crystim.change_dtype('float')

        # Set calibration to same as signal
        x = crystim.axes_manager.signal_axes[0]
        y = crystim.axes_manager.signal_axes[1]

        x.name = 'x'
        x.scale = self.axes_manager.navigation_axes[0].scale
        x.units = 'nm'

        y.name = 'y'
        y.scale = self.axes_manager.navigation_axes[0].scale
        y.units = 'nm'

        return crystim

    def calculate_cartesian_coordinates(self, accelerating_voltage, camera_length,
                                        *args, **kwargs):
        """Get cartesian coordinates of the diffraction vectors.

        Parameters
        ----------
        accelerating_voltage : float
            The acceleration voltage with which the data was acquired.
        camera_length : float
            The camera length in meters.
        """
        # Imported here to avoid circular dependency
        from diffsims.utils.sim_utils import get_electron_wavelength
        wavelength = get_electron_wavelength(accelerating_voltage)
        self.cartesian = self.map(detector_to_fourier,
                                  wavelength=wavelength,
                                  camera_length=camera_length * 1e10,
                                  inplace=False,
                                  parallel=False,  # TODO: For testing
                                  *args, **kwargs)
        transfer_navigation_axes(self.cartesian, self)
