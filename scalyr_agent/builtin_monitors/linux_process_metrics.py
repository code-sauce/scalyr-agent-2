# Copyright 2014 Scalyr Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ------------------------------------------------------------------------
#
# A ScalyrMonitor that collects metrics on a running Linux process.  The
# collected metrics include CPU and memory usage.
#
# Note, this can be run in standalone mode by:
#     python -m scalyr_agent.run_monitor scalyr_agent.builtin_monitors.linux_process_metrics -c "{ pid:1234}"
#
#   where 1234 is the process id of the target process.
# See documentation for other ways to match processes.
#
# author:  Steven Czerwinski <czerwin@scalyr.com>

__author__ = 'czerwin@scalyr.com'

from scalyr_agent import ScalyrMonitor, BadMonitorConfiguration

from subprocess import Popen, PIPE

from collections import defaultdict, namedtuple
import os
import re
import time

from scalyr_agent import define_config_option, define_metric, define_log_field

__monitor__ = __name__

define_config_option(__monitor__, 'module',
                     'Always ``scalyr_agent.builtin_monitors.linux_process_metrics``',
                     convert_to=str, required_option=True)
define_config_option(__monitor__, 'commandline',
                     'A regular expression which will match the command line of the process you\'re interested in, as '
                     'shown in the output of ``ps aux``. (If multiple processes match the same command line pattern, '
                     'only one will be monitored.)', default=None, convert_to=str)
define_config_option(__monitor__, 'pid',
                     'The pid of the process from which the monitor instance will collect metrics.  This is ignored '
                     'if the ``commandline`` is specified.',
                     default=None, convert_to=str)
define_config_option(__monitor__, 'id',
                     'Included in each log message generated by this monitor, as a field named ``instance``. Allows '
                     'you to distinguish between values recorded by different monitors.',
                     required_option=True, convert_to=str)

define_metric(__monitor__, 'app.cpu',
              'User-mode CPU usage, in 1/100ths of a second.',
              extra_fields={'type': 'user'}, unit='secs:0.01', cumulative=True)

define_metric(__monitor__, 'app.cpu',
              'System-mode CPU usage, in 1/100ths of a second.', extra_fields={'type': 'system'}, unit='secs:0.01',
              cumulative=True)

define_metric(__monitor__, 'app.uptime',
              'Process uptime, in milliseconds.', unit='milliseconds', cumulative=True)

define_metric(__monitor__, 'app.threads',
              'The number of threads being used by the process.')

define_metric(__monitor__, 'app.nice',
              'The nice value for the process.')

define_metric(__monitor__, 'app.mem.bytes',
              'Virtual memory usage, in bytes.', extra_fields={'type': 'vmsize'}, unit='bytes')

define_metric(__monitor__, 'app.mem.bytes',
              'Resident memory usage, in bytes.', extra_fields={'type': 'resident'}, unit='bytes')

define_metric(__monitor__, 'app.mem.bytes',
              'Peak virtual memory usage, in bytes.', extra_fields={'type': 'peak_vmsize'}, unit='bytes')

define_metric(__monitor__, 'app.mem.bytes',
              'Peak resident memory usage, in bytes.', extra_fields={'type': 'peak_resident'}, unit='bytes')

define_metric(__monitor__, 'app.disk.bytes',
              'Total bytes read from disk.', extra_fields={'type': 'read'}, unit='bytes', cumulative=True)

define_metric(__monitor__, 'app.disk.requests',
              'Total disk read requests.', extra_fields={'type': 'read'}, unit='bytes', cumulative=True)

define_metric(__monitor__, 'app.disk.bytes',
              'Total bytes written to disk.', extra_fields={'type': 'write'}, unit='bytes', cumulative=True)

define_metric(__monitor__, 'app.disk.requests',
              'Total disk write requests.', extra_fields={'type': 'write'}, unit='bytes', cumulative=True)

define_metric(__monitor__, 'app.io.fds',
              'The number of open file descriptors help by process.', extra_fields={'type': 'open'})

define_log_field(__monitor__, 'monitor', 'Always ``linux_process_metrics``.')
define_log_field(__monitor__, 'instance', 'The ``id`` value from the monitor configuration, e.g. ``tomcat``.')
define_log_field(__monitor__, 'app', 'Same as ``instance``; provided for compatibility with the original Scalyr Agent.')
define_log_field(__monitor__, 'metric', 'The name of a metric being measured, e.g. "app.cpu".')
define_log_field(__monitor__, 'value', 'The metric value.')


class MetricPrinter:
    """Helper class that emits metrics for the specified monitor.
    """
    def __init__(self, logger, monitor_id):
        """Initializes the class.

        @param logger: The logger instances to use to report metrics.
        @param monitor_id: The id of the monitor instance, used to identify all metrics reported through the logger.
        @type logger: scalyr_logging.AgentLogger
        @type monitor_id: str
        """
        self.__logger = logger
        self.__id = monitor_id


class BaseReader:
    """The base class for all readers.  Each derived reader class is responsible for
    collecting a set of statistics from a single per-process file from the /proc file system
    such as /proc/self/stat.  We create an instance for a reader for each application
    that is being monitored.  This instance is created once and then used from
    then on until the monitored process terminates.
    """
    def __init__(self, pid, monitor_id, logger, file_pattern):
        """Initializes the base class.

        @param pid: The id of the process being monitored.
        @param monitor_id: The id of the monitor instance, used to identify all metrics reported through the logger.
        @param logger: The logger instance to use for reporting the metrics.
        @param file_pattern: A pattern that is used to determine the path of the file to be read.  It should
            contain a %d in it which will be replaced by the process id number.  For example, "/proc/%d/stat"

        @type pid: int
        @type monitor_id: str
        @type logger: scalyr_logging.AgentLogger
        @type file_pattern: str
        """
        self._pid = pid
        self._id = monitor_id
        self._file_pattern = file_pattern
        # The file object to be read.  We always keep this open and just seek to zero when we need to
        # re-read it.  Some of the /proc files do better with this approach.
        self._file = None
        # The time we last collected the metrics.
        self._timestamp = None
        # True if the reader failed for some unrecoverable error.
        self._failed = False
        self._logger = logger
        self._metric_printer = MetricPrinter(logger, monitor_id)

    def run_single_cycle(self, collector=None):
        """
        Runs a single cycle of the sample collection.

        It should read the monitored file and extract all metrics.
        :param collector: Optional - a dictionary to collect the metric values
        :return: None or the optional collector with collected metric values
        """

        self._timestamp = int(time.time())

        # There are certain error conditions, such as the system not supporting
        # a particular proc file type, that we will never recover from.  So,
        # just always early exit.
        if self._failed:
            return

        filename = self._file_pattern % self._pid

        if not collector:
            collector = {}
        if self._file is None:
            try:
                self._file = open(filename, "r")
            except IOError, e:
                print e
                # We take a simple approach.  If we don't find the file or
                # don't have permissions for it, then just don't collect this
                # stat from now on.  If the user changes the configuration file
                # we will try again to read the file then.
                self._failed = True
                if e.errno == 13:
                    self._logger.error("The agent does not have permission to read %s.  "
                                       "Maybe you should run it as root.", filename)
                elif e.errno == 2:
                    self._logger.error("The agent cannot read %s.  Your system may not support that proc file type",
                                       filename)
                else:
                    raise e

        if self._file is not None:
            try:
                self._file.seek(0)
                return self.gather_sample(self._file, collector=collector)
            except IOError, e:
                print e
                self._logger.error( "Error gathering sample for file: '%s'\n\t%s" % (filename, str( e ) ) );

                # close the file. This will cause the file to be reopened next call to run_single_cycle
                self.close()
        return collector

    def gather_sample(self, my_file, collector=None):
        """Reads the metrics from the file and records them.

        Derived classes must override this method to perform the actual work of
        collecting their specific samples.

        @param my_file: The file to read.
        @type my_file: FileIO
        @param collector: The optional collector dictionary
        @type collector: None or dict
        """

        pass

    def close(self):
        """Closes any files held open by this reader."""
        try:
            self._failed = True
            if self._file is not None:
                self._file.close()
            self._failed = False
        finally:
            self._file = None


class StatReader(BaseReader):
    """Reads and records statistics from the /proc/$pid/stat file.

    The recorded metrics are listed below.  They all also have an app=[id] field as well.
      app.cpu type=user:     number of 1/100ths seconds of user cpu time
      app.cpu type=system:   number of 1/100ths seconds of system cpu time
      app.uptime:            number of milliseconds of uptime
      app.threads:           the number of threads being used by the process
      app.nice:              the nice value for the process
    """

    def __init__(self, pid, monitor_id, logger):
        """Initializes the reader.

        @param pid: The id of the process
        @param monitor_id: The id of the monitor instance
        @param logger: The logger to use to record metrics

        @type pid: int
        @type monitor_id: str
        @type logger: scalyr_agent.AgentLogger
        """

        BaseReader.__init__(self, pid, monitor_id, logger, "/proc/%ld/stat")
        # Need the number of jiffies_per_sec for this server to calculate some times.
        self._jiffies_per_sec = os.sysconf(os.sysconf_names['SC_CLK_TCK'])
        # The when this machine was last booted up.  This is required to calculate the process uptime.
        self._boot_time_ms = None

    def __calculate_time_cs(self, jiffies):
        """Returns the number of centiseconds (1/100ths secs) for the given number of jiffies (a weird timing unit
        used the kernel).

        @param jiffies: The number of jiffies.
        @type jiffies: int

        @return: The number of centiseconds for the specified number of jiffies.
        @rtype: int
        """

        return int((jiffies * 100.0) / self._jiffies_per_sec)

    def calculate_time_ms(self, jiffies):
        """Returns the number of milliseconds for the given number of jiffies (a weird timing unit
        used the kernel).

        @param jiffies: The number of jiffies.
        @type jiffies: int

        @return: The number of milliseconds for the specified number of jiffies.
        @rtype: int
        """

        return int((jiffies * 1000.0) / self._jiffies_per_sec)

    def __get_uptime_ms(self):
        """Returns the number of milliseconds the system has been up.

        @return: The number of milliseconds the system has been up.
        @rtype: int
        """

        if self._boot_time_ms is None:
            # We read /proc/uptime once to get the current boot time.
            uptime_file = None
            try:
                uptime_file = open("/proc/uptime", "r")
                # The first number in the file is the number of seconds since
                # boot time.  So, we just use that to calculate the milliseconds
                # past epoch.
                self._boot_time_ms = int(time.time()) * 1000 - int(float(uptime_file.readline().split()[0]) * 1000.0)
            finally:
                if uptime_file is not None:
                    uptime_file.close()

        # Calculate the uptime by just taking current time and subtracting out
        # the boot time.
        return int(time.time()) * 1000 - self._boot_time_ms

    def gather_sample(self, stat_file, collector=None):
        """Gathers the metrics from the stat file.

        @param stat_file: The file to read.
        @type stat_file: FileIO
        @param collector: Optional collector dictionary
        @type collector: None or dict
        """
        if not collector:
            collector = {}
        # The file format is just a single line of all the fields.
        line = stat_file.readlines()[0]
        # Chop off first part which is the pid and executable file. The
        # executable file is terminated with a paren so just search for that.
        line = line[(line.find(") ")+2):]
        fields = line.split()
        # Then the fields we want are just at fixed field positions in the
        # string.  Just grab them.

        process_uptime = self.__get_uptime_ms() - self.calculate_time_ms(int(fields[19]))

        collector.update({
            ('app.cpu', 'user'): self.__calculate_time_cs(int(fields[11])),
            ('app.cpu', 'system'): self.__calculate_time_cs(int(fields[12])),
            ('app.uptime', None): process_uptime,
            ('app.nice', None): float(fields[16]),
            ('app.threads', None): int(fields[17]),
        })
        return collector


class StatusReader(BaseReader):
    """Reads and records statistics from the /proc/$pid/status file.

    The recorded metrics are listed below.  They all also have an app=[id] field as well.
      app.mem.bytes type=vmsize:        the number of bytes of virtual memory in use
      app.mem.bytes type=resident:      the number of bytes of resident memory in use
      app.mem.bytes type=peak_vmsize:   the maximum number of bytes used for virtual memory for process
      app.mem.bytes type=peak_resident: the maximum number of bytes of resident memory ever used by process
    """

    def __init__(self, pid, monitor_id, logger):
        """Initializes the reader.

        @param pid: The id of the process
        @param monitor_id: The id of the monitor instance
        @param logger: The logger to use to record metrics

        @type pid: int
        @type monitor_id: str
        @type logger: scalyr_agent.AgentLogger
        """
        BaseReader.__init__(self, pid, monitor_id, logger, "/proc/%ld/status")

    def gather_sample(self, stat_file, collector=None):
        """Gathers the metrics from the status file.

        @param stat_file: The file to read.
        @type stat_file: FileIO
        @param collector: Optional collector dictionary
        @type collector: None or dict
        """

        if not collector:
            collector = {}

        for line in stat_file:
            # Each line has a format of:
            # Tag: Value
            #
            # We parse out all lines looking like that and match the stats we care about.
            m = re.search('^(\w+):\s*(\d+)', line)
            if m is None:
                continue

            field_name = m.group(1)
            int_value = int(m.group(2))
            # FDSize is not the same as the number of open file descriptors. Disable
            # for now.
            # if field_name == "FDSize":
            #     self.print_sample("app.fd", int_value)

            collector.update({
                ('app.mem.bytes', 'vmsize'): int_value * 1024,
                ('app.mem.bytes', 'peak_vmsize'): int_value * 1024,
                ('app.mem.bytes', 'resident'): int_value * 1024,
                ('app.mem.bytes', 'peak_resident'): int_value * 1024
            })
            return collector


# Reads stats from /proc/$pid/io.
class IoReader(BaseReader):
    """Reads and records statistics from the /proc/$pid/io file.  Note, this io file is only supported on
    kernels 2.6.20 and beyond, but that kernel has been around since 2007.

    The recorded metrics are listed below.  They all also have an app=[id] field as well.
      app.disk.bytes type=read:         the number of bytes read from disk
      app.disk.requests type=read:      the number of disk requests.
      app.disk.bytes type=write:        the number of bytes written to disk
      app.disk.requests type=write:     the number of disk requests.
    """
    def __init__(self, pid, monitor_id, logger):
        """Initializes the reader.

        @param pid: The id of the process
        @param monitor_id: The id of the monitor instance
        @param logger: The logger to use to record metrics

        @type pid: int
        @type monitor_id: str
        @type logger: scalyr_agent.AgentLogger
        """
        BaseReader.__init__(self, pid, monitor_id, logger, "/proc/%ld/io")

    def gather_sample(self, stat_file, collector=None):
        """Gathers the metrics from the io file.

        @param stat_file: The file to read.
        @type stat_file: FileIO
        @param collector: Optional collector dictionary
        @type collector: None or dict
        """

        if not collector:
            collector = {}

        # File format is single value per line with "fieldname:" prefix.
        for x in stat_file:
            fields = x.split()
            if len( fields ) == 0:
                continue
            if not collector:
                collector = {}
            if fields[0] == "rchar:":
                collector.update({("app.disk.bytes", "read"): int(fields[1])})
            elif fields[0] == "syscr:":
                collector.update({("app.disk.requests", "read"): int(fields[1])})
            elif fields[0] == "wchar:":
                collector.update({("app.disk.bytes", "write"): int(fields[1])})
            elif fields[0] == "syscw:":
                collector.update({("app.disk.requests", "write"): int(fields[1])})
        return collector


class NetStatReader(BaseReader):
    """NOTE:  This is not a per-process stat file, so this reader is DISABLED for now.

    Reads and records statistics from the /proc/$pid/net/netstat file.

    The recorded metrics are listed below.  They all also have an app=[id] field as well.
      app.net.bytes type=in:  The number of bytes read in from the network
      app.net.bytes type=out:  The number of bytes written to the network
      app.net.tcp_retransmits:  The number of retransmits
    """
    def __init__(self, pid, monitor_id, logger):
        """Initializes the reader.

        @param pid: The id of the process
        @param monitor_id: The id of the monitor instance
        @param logger: The logger to use to record metrics

        @type pid: int
        @type monitor_id: str
        @type logger: scalyr_agent.AgentLogger
        """

        BaseReader.__init__(self, pid, monitor_id, logger, "/proc/%ld/net/netstat")

    def gather_sample(self, stat_file, collector=None):
        """Gathers the metrics from the netstate file.

        @param stat_file: The file to read.
        @type stat_file: FileIO
        @param collector: Optional collector dictionary
        @type collector: None or dict
        """

        # This file format is weird.  Each set of stats is outputted in two
        # lines.  First, a header line that list the field names.  Then a
        # a value line where each value is specified in the appropriate column.
        # You have to match the column name from the header line to determine
        # what that column's value is.  Also, each pair of lines is prefixed
        # with the same name to make it clear they are tied together.
        all_lines = stat_file.readlines()
        # We will create an array of all of the column names in field_names
        # and all of the corresponding values in field_values.
        field_names = []
        field_values = []

        # To simplify the stats, we add together the two forms of retransmit
        # I could find in the netstats.  Those to fast retransmit Reno and those
        # to selective Ack.
        retransmits = 0
        found_retransmit_metric = False

        # Read over lines, looking at adjacent lines.  If their row names match,
        # then append their column names and values to field_names
        # and field_values.  This will break if the two rows are not adjacent
        # but I do not think that happens in practice.  If it does, we just
        # won't report the stats.
        for i in range(0, len(all_lines) - 1):
            names_split = all_lines[i].split()
            values_split = all_lines[i+1].split()
            # Check the row names are the same.
            if names_split[0] == values_split[0] and len(names_split) == len(values_split):
                field_names.extend(names_split)
                field_values.extend(values_split)

        if not collector:
            collector = {}

        # Now go back and look for the actual stats we care about.
        for i in range(0, len(field_names)):
            if field_names[i] == "InOctets":
                collector.update({("app.net.bytes", "in"): field_values[i]})
            elif field_names[i] == "OutOctets":
                collector.update({("app.net.bytes", "out"): field_values[i]})
            elif field_names[i] == "TCPRenoRecovery":
                retransmits += int(field_values[i])
                found_retransmit_metric = True
            elif field_names[i] == "TCPSackRecovery":
                retransmits += int(field_values[i])
                found_retransmit_metric = True

        # If we found both forms of retransmit, add them up.
        if found_retransmit_metric:
            collector.update({("app.net.tcp_retransmits", None): retransmits})
        return collector


class SockStatReader(BaseReader):
    """NOTE:  This is not a per-process stat file, so this reader is DISABLED for now.

    Reads and records statistics from the /proc/$pid/net/sockstat file.

    The recorded metrics are listed below.  They all also have an app=[id] field as well.
      app.net.sockets_in_use type=*:  The number of sockets in use
    """
    def __init__(self, pid, monitor_id, logger):
        BaseReader.__init__(self, pid, monitor_id, logger, "/proc/%ld/net/sockstat")

    def gather_sample(self, stat_file, collector=None):
        """Gathers the metrics from the sockstat file.

        @param stat_file: The file to read.
        @type stat_file: FileIO
        @param collector: Optional collector dictionary
        @type collector: None or dict
        """

        if not collector:
            collector = {}

        for line in stat_file:
            # We just look for the different "inuse" lines and output their
            # socket type along with the count.
            m = re.search('(\w+): inuse (\d+)', line)
            if m is not None:
                collector.update({("app.net.sockets_in_use", m.group(1).lower()): int(m.group(2))})
        return collector


# Reads stats from /proc/$pid/fd.
class FileDescriptorReader:
    """Reads and records statistics from the /proc/$pid/fd directory.  Essentially it just counts the number of entries.

    The recorded metrics are listed below.  They all also have an app=[id] field as well.
      app.io.fds type=open:         the number of open file descriptors
    """
    def __init__(self, pid, monitor_id, logger):
        """Initializes the reader.

        @param pid: The id of the process
        @param monitor_id: The id of the monitor instance
        @param logger: The logger to use to record metrics

        @type pid: int
        @type monitor_id: str
        @type logger: scalyr_agent.AgentLogger
        """
        self.__pid = pid
        self.__monitor_id = monitor_id
        self.__logger = logger
        self.__metric_printer = MetricPrinter(logger, monitor_id)
        self.__path = '/proc/%ld/fd' % pid

    def run_single_cycle(self, collector=None):
        """

        @return:
        @rtype:
        """

        num_fds = len(os.listdir(self.__path))
        if not collector:
            collector = {}
        collector.update({
            ('app.io.fds', 'open'): num_fds
        })
        return collector

    def close(self):
        pass


class ProcessTracker(object):
    """
    This class is responsible for gathering the metrics for a process.
    Given a process id, it procures and stores different metrics using
    metric readers (deriving from BaseReader)

    This tracker records the following metrics:
      app.cpu type=user:                the number of 1/100ths seconds of user cpu time
      app.cpu type=system:              the number of 1/100ths seconds of system cpu time
      app.uptime:                       the number of milliseconds of uptime
      app.threads:                      the number of threads being used by the process
      app.nice:                         the nice value for the process
      app.mem.bytes type=vmsize:        the number of bytes of virtual memory in use
      app.mem.bytes type=resident:      the number of bytes of resident memory in use
      app.mem.bytes type=peak_vmsize:   the maximum number of bytes used for virtual memory for process
      app.mem.bytes type=peak_resident: the maximum number of bytes of resident memory ever used by process
      app.disk.bytes type=read:         the number of bytes read from disk
      app.disk.requests type=read:      the number of disk requests.
      app.disk.bytes type=write:        the number of bytes written to disk
      app.disk.requests type=write:     the number of disk requests.
      app.io.fds type=open:             the number of file descriptors held open by the process
    """

    def __init__(self, pid, logger, monitor_id=None):
        self.pid = pid
        self.monitor_id = monitor_id
        self._logger = logger
        self.gathers = []

    def set_gathers(self):
        """
        Sets the id of the process for which this monitor instance should record metrics.
        """

        for gather in self.gathers:
            gather.close()

        self.gathers.append(StatReader(self.pid, self.monitor_id, self._logger))
        self.gathers.append(StatusReader(self.pid, self.monitor_id, self._logger))
        self.gathers.append(IoReader(self.pid, self.monitor_id, self._logger))
        self.gathers.append(FileDescriptorReader(self.pid, self.monitor_id, self._logger))

        # TODO: Re-enable these if we can find a way to get them to truly report
        # per-app statistics.
        #        self.gathers.append(NetStatReader(self.pid, self.id, self._logger))
        #        self.gathers.append(SockStatReader(self.pid, self.id, self._logger))

    def __is_running(self):
        """Returns true if the current process is still running.

        @return:  True if the monitored process is still running.
        @rtype: bool
        """
        try:
            # signal flag 0 does not actually try to kill the process but does an error
            # check that is useful to see if a process is still running.
            os.kill(self.pid, 0)
            return True
        except OSError, e:
            # Errno #3 corresponds to the process not running.  We could get
            # other errors like this process does not have permission to send
            # a signal to self.pid.  But, if that error is returned to us, we
            # know the process is running at least, so we ignore the error.
            return e.errno != 3

    def collect(self):
        """
        Collects the metrics from the gathers
        """

        collector = {}
        for gather in self.gathers:
            try:
                collector.update(gather.run_single_cycle(collector=collector))
            except Exception as ex:
                self._logger.exception(
                    'Exception while collecting metrics for PID: %s of type: %s. Details: %s',
                    self.pid, type(gather), repr(ex)
                )
        return collector


Metric = namedtuple('Metric', ['name', 'type'])


class ProcessMonitor(ScalyrMonitor):
    """A Scalyr agent monitor that records metrics about a running process.

    To configure this monitor, you need to provide an id for the instance to identify which process the metrics
    belong to in the logs and a regular expression to match against the list of running processes to determine which
    process should be monitored.

    Example:
      monitors: [{
         module: "builtin_monitors.linux_process_metrics".
         id: "tomcat",
         commandline: "java.*tomcat",
      }]

    Instead of 'commandline', you may also define the 'pid' field which should be set to the id of the process to
    monitor.  However, since ids can change over time, it's better to use the commandline matcher.  The 'pid' field
    is mainly used the linux process monitor run to monitor the agent itself.


    In additional to the fields listed above, each metric will also have a field 'app' set to the monitor id to specify
    which process the metric belongs to.

    You can run multiple instances of this monitor per agent to monitor different processes.
    """

    def _initialize(self):
        """Performs monitor-specific initialization."""
        # The id of the process being monitored, if one has been matched.
        self.__pid = None

        self.__id = self._config.get('id', required_field=True, convert_to=str)
        self.__commandline_matcher = self._config.get('commandline', default=None, convert_to=str)
        self.__target_pids = self._config.get('pid', default=None, convert_to=str)

        # convert target pid into a list. Target pids can be a single pid or a CSV of pids
        self.__target_pids = [int(x.strip()) for x in self.__target_pids.split(',')] if self.__target_pids else []
        self.__target_pids = set(self.__target_pids)

        # history of all metrics
        self.__metrics_history = defaultdict(dict)
        # running total of metric values
        self.__running_total_metrics = {}

        if not (self.__commandline_matcher or self.__target_pids):
            raise BadMonitorConfiguration(
                'At least one of the following fields must be provide: commandline or pid',
                'commandline'
            )

        # Make sure to set our configuration so that the proper parser is used.
        self.log_config = {
            'parser': 'agent-metrics',
            'path': 'linux_process_metrics.log',
        }

    def _record_metrics(self, pid, metrics):
        """
        For a process, record the metrics in a historical metrics collector
        Collects the historical result of each metric per process in __metrics_history
        which has form:

        {
          '<process id>: {
                        <metric name>: [<metric at time 0>, <metric at time 1>.... ],
                        <metric name>: [<metric at time 0>, <metric at time 1>.... ],
                        }

          '<process id>: {
                        <metric name>: [<metric at time 0>, <metric at time 1>.... ],
                        <metric name>: [<metric at time 0>, <metric at time 1>.... ],
                        }
        }

        @param pid: Process ID
        @param metrics: Collected metrics of the process
        @type pid: int
        @type metrics: dict
        :return: None
        """
        for (_metric_name, _metric_type), _metric_value in metrics.items():
            if not self.__metrics_history[pid].get((_metric_name, _metric_type)):
                self.__metrics_history[pid][(_metric_name, _metric_type)] = []
            self.__metrics_history[pid][(_metric_name, _metric_type)].append(_metric_value)
            # only keep the last 2 running history for any metric
            self.__metrics_history[pid][(_metric_name, _metric_type)] =\
                self.__metrics_history[pid][(_metric_name, _metric_type)][-2:]

    def _reset_absolute_metrics(self):
        for pid, process_metrics in self.__metrics_history.items():
            for (_metric_name, _metric_type), _metric_values in process_metrics.items():
                if _metric_name not in ('app.cpu', ):
                    self.__running_total_metrics[(_metric_name, _metric_type)] = 0

    def _calculate_running_total(self, running_pids):
        """
        Calculates the running total metric values based on the current running processes
        and the historical metric record
        @param running_pids: list of running process ids
        @type running_pids: list
        """

        # using the historical values, calculate the running total
        # there are two kinds of metrics:
        # a) monotonically increasing metrics - only the delta of the last 2 recorded values is used (eg cpu cycles)
        # b) absolute metrics - the last absolute value is used

        running_pids_set = set(running_pids)

        for pid, process_metrics in self.__metrics_history.items():
            for (_metric_name, _metric_type), _metric_values in process_metrics.items():
                if not self.__running_total_metrics.get((_metric_name, _metric_type)):
                    self.__running_total_metrics[(_metric_name, _metric_type)] = 0
                if _metric_name == 'app.cpu':
                    if pid in running_pids_set:
                        if len(_metric_values) < 2:
                            self.__running_total_metrics[(_metric_name, _metric_type)] = 0
                        else:
                            self.__running_total_metrics[(_metric_name, _metric_type)] += \
                            (_metric_values[-1] - _metric_values[-2])
                    else:
                        # remove the contribution of the dead process id
                        if len(_metric_values) >= 2:
                            self.__running_total_metrics[(_metric_name, _metric_type)] -= \
                                (_metric_values[-1] - _metric_values[-2])
                else:
                    self.__running_total_metrics[(_metric_name, _metric_type)] += _metric_values[-1]

        # once this is done, for any dead process that has already been accounted for in the delta
        # calculation above, remove the entries for the process id
        all_pids = self.__metrics_history.keys()
        for _pid_to_remove in list(set(all_pids) - set(running_pids)):
            # for all the absolute metrics, decrease the count that the dead processes accounted for


            del self.__metrics_history[_pid_to_remove]

    def gather_sample(self):
        """Collect the per-process tracker for the monitored process(es).

        For multiple processes, there are a few cases we need to poll for:

        For `n` processes, if there are < n running processes at any given point,
        we should see if there are any pids or matching expression that gives pids
        that is currently not running, if so, get its tracker and run it.
        It is possible that one or more processes ran its course, but we can't assume that
        and we should keep polling for it.
        """

        trackers = []
        for _pid in list(self.__select_processes()):
            trackers.append(ProcessTracker(_pid, self._logger, self.__id))
        for _tracker in trackers:
            _tracker.set_gathers()
            _metrics = _tracker.collect()
            self._record_metrics(_tracker.pid, _metrics)

        self._reset_absolute_metrics()
        self._calculate_running_total([x.pid for x in trackers])
        self.print_metrics()

    def print_metrics(self):
        # For backward compatibility, we also publish the monitor id as 'app' in all reported stats.  The old
        # Java agent did this and it is important to some dashboards.

        for (_metric_name, _metric_type), _metric_value in self.__running_total_metrics.items():
            print "emitting: \n", _metric_name, _metric_value, {'app': self.__id, 'type': _metric_type}
            extra = {'app': self.__id}
            if _metric_type:
                extra['type'] = _metric_type
            self._logger.emit_value(_metric_name, _metric_value, extra)

    def __select_processes(self):
        """Returns a set of the process ids of processes that fulfills the match criteria.

        This will either use the commandline matcher or the target pid to find the process.
        If no process is matched, an empty list is returned.

        @return: The process ids of the matching process, or []
        @rtype: [int] or []
        """

        sub_proc = None

        if self.__commandline_matcher:
            try:
                # Spawn a process to run ps and match on the command line.  We only output two
                # fields from ps.. the pid and command.
                sub_proc = Popen(['ps', 'ax', '-o', 'pid,command'],
                                 shell=False, stdout=PIPE)
                lines = sub_proc.stdout.readlines()
                matching_pids = set()
                for line in lines:
                    line = line.strip()
                    if line.find(' ') > 0:
                        pid = line[:line.find(' ')]
                        if pid.lower() == 'pid':
                            continue
                        pid = int(pid)
                        line = line[(line.find(' ') + 1):]
                        if re.search(self.__commandline_matcher, line) is not None:
                            matching_pids.add(pid)
                return matching_pids

            finally:
                # Be sure to wait on the spawn process.
                if sub_proc is not None:
                    sub_proc.wait()
        else:
            # See if the specified target pid is running.  If so, then return it.
            # Special case '$$' to mean this process.
            if self.__target_pids == '$$':
                pids = {os.getpid()}
            else:
                pids = self.__target_pids
            return pids


__all__ = [ProcessMonitor]
