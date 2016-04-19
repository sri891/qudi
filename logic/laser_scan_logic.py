# -*- coding: utf-8 -*-
"""
This file contains a QuDi logic module for controlling scans of the
fourth analog output channel.  It was originally written for
scanning laser frequency, but it can be used to control any parameter
in the experiment that is voltage controlled.  The hardware
range is typically -10 to +10 V.

QuDi is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

QuDi is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with QuDi. If not, see <http://www.gnu.org/licenses/>.

Copyright (C) 2015 Kay D. Jahnke
Copyright (C) 2015 Jan M. Binder
Copyright (C) 2016 Lachlan J. Rogers
"""

from logic.generic_logic import GenericLogic
from pyqtgraph.Qt import QtCore
from core.util.mutex import Mutex
from collections import OrderedDict
import numpy as np
import time
import datetime


class VoltageScanningLogic(GenericLogic):

    """This logic module controls scans of DC voltage on the fourth analog
    output channel of the NI Card.  It collects countrate as a function of voltage.
    """

    sig_data_updated = QtCore.Signal()

    _modclass = 'voltagescanninglogic'
    _modtype = 'logic'

    # declare connectors
    _in = {'confocalscanner1': 'ConfocalScannerInterface',
           'savelogic': 'SaveLogic',
           }
    _out = {'voltagescanninglogic': 'VoltageScanningLogic'}

    signal_change_voltage = QtCore.Signal()
    signal_scan_next_line = QtCore.Signal()

    def __init__(self, manager, name, config, **kwargs):
        """ Create VoltageScanningLogic object with connectors.

          @param object manager: Manager object thath loaded this module
          @param str name: unique module name
          @param dict config: module configuration
          @param dict kwargs: optional parameters
        """
        # declare actions for state transitions
        state_actions = {'onactivate': self.activation, 'ondeactivate': self.deactivation}
        super().__init__(manager, name, config, state_actions, **kwargs)

        # locking for thread safety
        self.threadlock = Mutex()

        self.stopRequested = False

    def activation(self, e):
        """ Initialisation performed during activation of the module.

          @param object e: Fysom state change event
        """
        self._scanning_device = self.connector['in']['confocalscanner1']['object']
        self._save_logic = self.connector['in']['savelogic']['object']

        # Reads in the maximal scanning range. The unit of that scan range is
        # micrometer!
        self.a_range = self._scanning_device.get_position_range()[3]

        # Initialise the current position of all four scanner channels.
        self.current_position = self._scanning_device.get_scanner_position()

        # initialise the range for scanning
        self.scan_range = [self.a_range[0] / 10, self.a_range[1] / 10]

        # Sets the current position to the center of the maximal scanning range
        self._current_v = (self.a_range[0] + self.a_range[1]) / 2.

        # Sets connections between signals and functions
        self.signal_change_voltage.connect(self._change_voltage, QtCore.Qt.QueuedConnection)
        self.signal_scan_next_line.connect(self._do_next_line, QtCore.Qt.QueuedConnection)

        # Initialization of internal counter for scanning
        self._scan_counter = 0

        # Keep track of scan direction
        self.upwards_scan = True

        # calculated number of points in a scan, depends on speed and max step size
        self._num_of_steps = 50  # initialising.  This is calculated for a given ramp.
        #############################
        # Configurable parameters

        self.number_of_repeats = 10

        # TODO: allow configuration with respect to measurement duration
        self.acquire_time = 20  # seconds

        # default values for clock frequency and slowness
        # slowness: steps during retrace line
        self._clock_frequency = 500.
        self._goto_speed = 0.01  # volt / second
        self._scan_speed = 0.01  # volt / second
        self._smoothing_steps = 10  # steps to accelerate between 0 and scan_speed
        self._max_step = 0.01  # volt

        ##############################

        # Initialie data matrix
        self._initialise_data_matrix(100)

    def deactivation(self, e):
        """ Deinitialisation performed during deactivation of the module.

          @param object e: Fysom state change event
        """
        pass

    def goto_voltage(self, volts=None):
        """Forwarding the desired output voltage to the scanning device.

        @param float volts: desired voltage (volts)

        @return int: error code (0:OK, -1:error)
        """
        # print(tag, x, y, z)
        # Changes the respective value
        if volts is not None:
            self._current_v = volts

        # Checks if the scanner is still running
        if self.getState() == 'locked' or self._scanning_device.getState() == 'locked':
            return -1
        else:
            self.signal_change_voltage.emit()
            return 0

    def _change_voltage(self):
        """ Threaded method to change the hardware voltage for a goto.

        @return int: error code (0:OK, -1:error)
        """
        ramp_scan = self._generate_ramp(self.get_current_voltage(), self._current_v, self._goto_speed)

        self._initialise_scanner()

        ignored_counts = self._scan_line(ramp_scan)

        self._close_scanner()

        return 0

    def set_clock_frequency(self, clock_frequency):
        """Sets the frequency of the clock

        @param int clock_frequency: desired frequency of the clock

        @return int: error code (0:OK, -1:error)
        """
        self._clock_frequency = int(clock_frequency)
        # checks if scanner is still running
        if self.getState() == 'locked':
            return -1
        else:
            return 0

    def _initialise_data_matrix(self, scan_length):
        """ Initializing the ODMR matrix plot. """

        self.scan_matrix = np.zeros((self.number_of_repeats, scan_length))

    def get_current_voltage(self):
        """returns current voltage of hardware device(atm NIDAQ 4th output)"""
        return self._scanning_device.get_scanner_position()[3]

    def _initialise_scanner(self):
        """Initialise the clock and locks for a scan"""

        self.lock()
        self._scanning_device.lock()

        returnvalue = self._scanning_device.set_up_scanner_clock(
            clock_frequency=self._clock_frequency)
        if returnvalue < 0:
            self._scanning_device.unlock()
            self.unlock()
            self.set_position('scanner')
            return -1

        returnvalue = self._scanning_device.set_up_scanner()
        if returnvalue < 0:
            self._scanning_device.unlock()
            self.unlock()
            self.set_position('scanner')
            return -1

        return 0

    def start_scanning(self, v_min=None, v_max=None):
        """Setting up the scanner device and starts the scanning procedure

        @return int: error code (0:OK, -1:error)
        """

        if v_min is not None:
            self.scan_range[0] = v_min
        if v_max is not None:
            self.scan_range[1] = v_max

        self._scan_counter = 0
        self.upwards_scan = True

        # TODO: Generate Ramps
        self._initialise_data_matrix(100)

        self.current_position = self._scanning_device.get_scanner_position()

        # Lock and set up scanner
        returnvalue = self._initialise_scanner()
        if returnvalue < 0:
            # TODO: error message
            return -1

        self.signal_scan_next_line.emit()
        return 0

    def stop_scanning(self):
        """Stops the scan

        @return int: error code (0:OK, -1:error)
        """
        with self.threadlock:
            if self.getState() == 'locked':
                self.stopRequested = True

        return 0

    def _close_scanner(self):
        """Close the scanner and unlock"""
        with self.threadlock:
            self.kill_scanner()
            self.stopRequested = False
            self.unlock()

    def _do_next_line(self):
        """If stopRequested then finish the scan, otherwise perform next repeat of the scan line

        """

        # stops scanning
        if self.stopRequested or self._scan_counter == self.number_of_repeats:
            if self.upwards_scan:
                ignored_counts = self._scan_line(self.scan_range[0], self.current_position[3])
            else:
                ignored_counts = self._scan_line(self.scan_range[0], self.current_position[3])
            self._close_scanner()
            return

        if self._scan_counter == 0:
            # move from current voltage to start of scan range.
            self.goto_voltage(self.scan_range[0])

        if self.upwards_scan:
            counts = self._scan_line(self.scan_range[0], self.scan_range[1])
            self.upwards_scan = False
        else:
            counts = self._scan_line(self.scan_range[1], self.scan_range[0])
            self.upwards_scan = True

        self.scan_matrix[self._scan_counter] = counts

        self._scan_counter += 1
        self.signal_scan_next_line.emit()

    def _generate_ramp(self, voltage1, voltage2, speed):
        """Generate a ramp vrom voltage1 to voltage2 that
        satisfies the speed, step, smoothing_steps parameters.  Smoothing_steps=0 means that the 
        ramp is just linear.

        @param float voltage1: voltage at start of ramp.

        @param float voltage2: voltage at end of ramp.
        """

        # It is much easier to calculate the smoothed ramp for just one direction (upwards),
        # and then to reverse it if a downwards ramp is required.

        v_min = min(voltage1, voltage2)
        v_max = max(voltage1, voltage2)

        # These values help simplify some of the mathematical expressions
        linear_v_step = speed / self._clock_frequency
        smoothing_range = self._smoothing_steps + 1

        # The voltage range covered while accelerating in the smoothing steps
        v_range_of_accel = sum(n * linear_v_step / smoothing_range
                               for n in range(0, smoothing_range)
                               )

        # Obtain voltage bounds for the linear part of the ramp
        v_min_linear = v_min + v_range_of_accel
        v_max_linear = v_max - v_range_of_accel

        num_of_linear_steps = np.rint((v_max_linear - v_min_linear) / linear_v_step)

        # Calculate voltage step values for smooth acceleration part of ramp
        smooth_curve = np.array([sum(n * linear_v_step / smoothing_range for n in range(1, N)) 
                                 for N in range(1, smoothing_range)
                                 ]
                                )

        accel_part = v_min + smooth_curve
        decel_part = v_max - smooth_curve[::-1]

        linear_part = np.linspace(v_min_linear, v_max_linear, num_of_linear_steps)

        ramp = np.hstack((accel_part, linear_part, decel_part))

        # Reverse if downwards ramp is required
        if voltage2 < voltage1:
            ramp = ramp[::-1]

        # Put the voltage ramp into a scan line for the hardware (4-dimension)
        spatial_pos = self._scanning_device.get_scanner_position()

        scan_line = np.vstack((
            np.linspace(spatial_pos[0], spatial_pos[0],
                        len(ramp)),
            np.linspace(spatial_pos[1], spatial_pos[1],
                        len(ramp)),
            np.linspace(spatial_pos[2], spatial_pos[2],
                        len(ramp)),
            ramp
            ))

        return scan_line

    def _scan_line(self, line_to_scan = None):
        """do a single voltage scan from voltage1 to voltage2

        """
        if line_to_scan is None:
            self.logMsg('Voltage scanning logic needs a line to scan!', msgType='error')
            return -1
        try:
            # scan of a single line
            counts_on_scan_line = self._scanning_device.scan_line(line_to_scan)

            return counts_on_scan_line

        except Exception as e:
            self.logMsg('The scan went wrong, killing the scanner.', msgType='error')
            self.stop_scanning()
            self.signal_scan_next_line.emit()
            raise e

    def kill_scanner(self):
        """Closing the scanner device.

        @return int: error code (0:OK, -1:error)
        """
        try:
            self._scanning_device.close_scanner()
            self._scanning_device.close_scanner_clock()
        except Exception as e:
            self.logExc('Could not even close the scanner, giving up.', msgType='error')
            raise e
        try:
            self._scanning_device.unlock()
        except Exception as e:
            self.logExc('Could not unlock scanning device.', msgType='error')

        return 0

    def save_data(self):
        """ Save the counter trace data and writes it to a file.

        @return int: error code (0:OK, -1:error)
        """

        self._saving_stop_time = time.time()

        filepath = self._save_logic.get_path_for_module(module_name='LaserScanning')
        filelabel = 'laser_scan'
        timestamp = datetime.datetime.now()

        # prepare the data in a dict or in an OrderedDict:
        data = OrderedDict()
        data = {'Wavelength (nm), Signal (counts/s)': np.array([self.histogram_axis, self.histogram]).transpose()}

        # write the parameters:
        parameters = OrderedDict()
        parameters['Bins (#)'] = self._bins
        parameters['Xmin (nm)'] = self._xmin
        parameters['XMax (nm)'] = self._xmax
        parameters['Start Time (s)'] = time.strftime('%d.%m.%Y %Hh:%Mmin:%Ss', time.localtime(self._acqusition_start_time))
        parameters['Stop Time (s)'] = time.strftime('%d.%m.%Y %Hh:%Mmin:%Ss', time.localtime(self._saving_stop_time))

        self._save_logic.save_data(data, filepath, parameters=parameters,
                                   filelabel=filelabel, timestamp=timestamp,
                                   as_text=True, precision=':.6f')  # , as_xml=False, precision=None, delimiter=None)

        filepath = self._save_logic.get_path_for_module(module_name='LaserScanning')
        filelabel = 'laser_scan_wavemeter'

        # prepare the data in a dict or in an OrderedDict:
        data = OrderedDict()
        data = {'Time (s), Wavelength (nm)': self._wavelength_data}
        # write the parameters:
        parameters = OrderedDict()
        parameters['Acquisition Timing (ms)'] = self._logic_acquisition_timing
        parameters['Start Time (s)'] = time.strftime('%d.%m.%Y %Hh:%Mmin:%Ss', time.localtime(self._acqusition_start_time))
        parameters['Stop Time (s)'] = time.strftime('%d.%m.%Y %Hh:%Mmin:%Ss', time.localtime(self._saving_stop_time))

        self._save_logic.save_data(data, filepath, parameters=parameters,
                                   filelabel=filelabel, timestamp=timestamp,
                                   as_text=True, precision=':.6f')  # , as_xml=False, precision=None, delimiter=None)

        filepath = self._save_logic.get_path_for_module(module_name='LaserScanning')
        filelabel = 'laser_scan_counts'

        # prepare the data in a dict or in an OrderedDict:
        data = OrderedDict()
        data = {'Time (s),Signal (counts/s)': self._counter_logic._data_to_save}

        # write the parameters:
        parameters = OrderedDict()
        parameters['Start counting time (s)'] = time.strftime('%d.%m.%Y %Hh:%Mmin:%Ss', time.localtime(self._counter_logic._saving_start_time))
        parameters['Stop counting time (s)'] = time.strftime('%d.%m.%Y %Hh:%Mmin:%Ss', time.localtime(self._saving_stop_time))
        parameters['Length of counter window (# of events)'] = self._counter_logic._count_length
        parameters['Count frequency (Hz)'] = self._counter_logic._count_frequency
        parameters['Oversampling (Samples)'] = self._counter_logic._counting_samples
        parameters['Smooth Window Length (# of events)'] = self._counter_logic._smooth_window_length

        self._save_logic.save_data(data, filepath, parameters=parameters,
                                   filelabel=filelabel, timestamp=timestamp,
                                   as_text=True, precision=':.6f')  # , as_xml=False, precision=None, delimiter=None)

        self.logMsg('Laser Scan saved to:\n{0}'.format(filepath),
                    msgType='status', importance=3)

        return 0