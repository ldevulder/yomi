# -*- coding: utf-8 -*-
#
# Author: Alberto Planas <aplanas@suse.com>
#
# Copyright 2019 SUSE LLC.
#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

'''
:maintainer:    Alberto Planas <aplanas@suse.com>
:maturity:      new
:depends:       None
:platform:      Linux
'''
from __future__ import absolute_import, print_function, unicode_literals
import logging
import re

from salt.exceptions import CommandExecutionError

import disk


log = logging.getLogger(__name__)

__virtualname__ = 'partitioned'

# Define not exported variables from Salt, so this can be imported as
# a normal module
try:
    __grains__
    __opts__
    __salt__
except NameError:
    __grains__ = {}
    __opts__ = {}
    __salt__ = {}


class EnumerateException(Exception):
    pass


def __virtual__():
    '''
    Partitioned depends on partition.mkpart module

    '''

    return 'partition.mkpart' in __salt__


def _check_label(device, label):
    '''
    Check if the label match with the device

    '''
    label = {
        'msdos': 'dos',
        }.get(label, label)
    res = __salt__['cmd.run'](['fdisk', '-l', device])
    return 'disklabel type: {}'.format(label) in res.lower()


def labeled(name, label):
    '''
    Make sure that the label of the partition is properly set.

    name
        Device name (/dev/sda, /dev/disk/by-id/scsi-...)

    label
        Label of the partition (usually 'gpt' or 'msdos')

    '''
    ret = {
        'name': name,
        'result': False,
        'changes': {},
        'comment': [],
    }

    if not label:
        ret['comment'].append('Label parameter is not optional')
        return ret

    if _check_label(name, label):
        ret['result'] = True
        ret['comment'].append('Label already set to {}'.format(label))
        return ret

    if __opts__['test']:
        ret['result'] = None
        ret['comment'].append(
            'Label will be set to {} in {}'.format(label, name))
        ret['changes']['label'] = 'Will be set to {}'.format(label)
        return ret

    __salt__['partition.mklabel'](name, label)

    if _check_label(name, label):
        ret['result'] = True
        msg = 'Label set to {} in {}'.format(label, name)
        ret['comment'].append(msg)
        ret['changes']['label'] = msg
    else:
        ret['comment'].append('Failed to set label to {}'.format(label))

    return ret


def _get_partition_type(device):
    '''
    Get partition type of each partition

    Return dictionary: {number: type, ...}

    '''
    cmd = 'parted -s {0} print'.format(device)
    out = __salt__['cmd.run_stdout'](cmd)
    types = re.findall(r'\s*(\d+).*(primary|extended|logical).*', out)
    return dict(types)


def _get_cached_info(device):
    '''
    Get the information of a device as a dictionary

    '''
    if not hasattr(_get_cached_info, 'info'):
        _get_cached_info.info = {}
    info = _get_cached_info.info

    if device not in info:
        info[device] = __salt__['partition.list'](device)['info']
    return info[device]


def _invalidate_cached_info():
    '''
    Invalidate the cached information about devices

    '''
    if hasattr(_get_cached_info, 'info'):
        delattr(_get_cached_info, 'info')


def _get_cached_partitions(device, unit='s'):
    '''
    Get the partitions as a dictionary

    '''
    # `partitions` will be used as a local cache, to avoid multiple
    # request of the same partition with the same units. Is a
    # dictionary where the key is the `unit`, as we will make request
    # of all partitions under this unit. This potentially can low the
    # complexity algorithm to amortized O(1).
    if not hasattr(_get_cached_partitions, 'partitions'):
        _get_cached_partitions.partitions = {}
        # There is a bug in `partition.list`, where `type` is storing
        # the file system information, to workaround this we get the
        # partition type using parted and attach it here.
        _get_cached_partitions.types = _get_partition_type(device)

    if device not in _get_cached_partitions.partitions:
        _get_cached_partitions.partitions[device] = {}
    partitions = _get_cached_partitions.partitions[device]

    if unit not in partitions:
        partitions[unit] = __salt__['partition.list'](device, unit=unit)
        # If the partition comes from a gpt disk, we assign the type
        # as 'primary'
        types = _get_cached_partitions.types
        for number, partition in partitions[unit]['partitions'].items():
            partition['type'] = types.get(number, 'primary')

    return partitions[unit]['partitions']


def _invalidate_cached_partitions():
    '''
    Invalidate the cached information about partitions

    '''
    if hasattr(_get_cached_partitions, 'partitions'):
        delattr(_get_cached_partitions, 'partitions')
        delattr(_get_cached_partitions, 'types')


OVERLAPPING_ERROR = 0.75


def _check_partition(device, number, part_type, start, end):
    '''
    Check if the proposed partition match the current one.

    Returns a tri-state value:
      - `True`: the proposed partition match
      - `False`: the proposed partition do not match
      - `None`: the proposed partition is a new partition
    '''
    # The `start` and `end` fields are expressed with units (the same
    # kind of units that `parted` allows). To make a fair comparison
    # we need to normalize each field to the same units that we can
    # use to read the current partitions. A good candidate is sector
    # ('s'). The problem is that we need to reimplement the same
    # conversion logic from `parted` here [1], as we need the same
    # round logic when we convert from 'MiB' to 's', for example.
    #
    # To avoid this duplicity of code we can do a trick: for each
    # field in the proposed partition we request a `partition.list`
    # with the same unit. We make `parted` to make the conversion for
    # us, in exchange for an slower algorithm.
    #
    # We can change it once we decide to take care of alignment.
    #
    # [1] Check libparted/unit.c

    number = str(number)
    partitions = _get_cached_partitions(device)
    if number not in partitions:
        return None

    if part_type != partitions[number]['type']:
        return False

    for value, name in ((start, 'start'), (end, 'end')):
        value, unit = disk.units(value)
        p_value = _get_cached_partitions(device, unit)[number][name]
        p_value = disk.units(p_value)[0]
        min_value = value - OVERLAPPING_ERROR
        max_value = value + OVERLAPPING_ERROR
        if not min_value <= p_value <= max_value:
            return False

    return True


def _get_first_overlapping_partition(device, start):
    '''
    Return the first partition that contains the start point.

    '''
    # Check if there is a partition in the system that start at
    # specified point.
    value, unit = disk.units(start)
    value += OVERLAPPING_ERROR

    partitions = _get_cached_partitions(device, unit)
    partition_number = None
    partition_start = 0
    for number, partition in partitions.items():
        p_start = disk.units(partition['start'])[0]
        p_end = disk.units(partition['end'])[0]
        if p_start <= value <= p_end:
            if partition_number is None or partition_start < p_start:
                partition_number = number
                partition_start = p_start
    return partition_number


def _get_partition_number(device, part_type, start, end):
    '''
    Return a partition number for a [start, end] range and a partition
    type.

    If the range is allocated and the partition type match, return the
    partition number. If the type do not match but is a logical
    partition inside an extended one, return the next partition
    number.

    If the range is not allocated, return the next partition number.

    '''

    unit = disk.units(start)[1]
    partitions = _get_cached_partitions(device, unit)

    # Check if there is a partition in the system that start or
    # containst the start point
    number = _get_first_overlapping_partition(device, start)
    if number:
        if partitions[number]['type'] == part_type:
            return number
        elif not (partitions[number]['type'] == 'extended'
                  and part_type == 'logical'):
            raise EnumerateException('Do not overlap partitions')

    def __primary_partition_free_slot(partitions, label):
        if label == 'msdos':
            max_primary = 4
        else:
            max_primary = 1024
        for i in range(1, max_primary + 1):
            i = str(i)
            if i not in partitions:
                return i

    # The partition is not already there, we guess the next number
    label = _get_cached_info(device)['partition table']
    if part_type == 'primary':
        candidate = __primary_partition_free_slot(partitions, label)
        if not candidate:
            raise EnumerateException('No free slot for primary partition')
        return candidate
    elif part_type == 'extended':
        if label == 'gpt':
            raise EnumerateException('Extended partitions not allowed in gpt')
        if 'extended' in (info['type'] for info in partitions.values()):
            raise EnumerateException('Already found a extended partition')
        candidate = __primary_partition_free_slot(partitions, label)
        if not candidate:
            raise EnumerateException('No free slot for extended partition')
        return candidate
    elif part_type == 'logical':
        if label == 'gpt':
            raise EnumerateException('Extended partitions not allowed in gpt')
        if 'extended' not in (part['type'] for part in partitions.values()):
            raise EnumerateException('Missing extended partition')
        candidate = max((int(part['number'])
                         for part in partitions.values()
                         if part['type'] == 'logical'), default=4)
        return str(candidate + 1)


def _get_partition_flags(device, number):
    '''
    Return the current list of flags for a partition.
    '''

    def _is_valid(flag):
        '''Return True if is a valid flag'''
        if flag == 'swap' or flag.startswith('type='):
            return False
        return True

    result = []
    number = str(number)
    partitions = __salt__['partition.list'](device)['partitions']
    if number in partitions:
        # In parted the field for flags is reused to mark other
        # situations, so we need to remove values that do not
        # represent flags
        flags = partitions[number]['flags'].split(', ')
        result = [flag for flag in flags if flag and _is_valid(flag)]
    return result


def mkparted(name, part_type, fs_type=None, start=None, end=None, flags=None):
    '''
    Make sure that a partition is allocated in the disk.

    name
        Device or partition name. If the name is like /dev/sda, parted
        will take care of creating the partition on the next slot. If
        the name is like /dev/sda1, we will consider partition 1 as a
        reference for the match.

    part_type
        Type of partition, should be one of "primary", "logical", or
        "extended".

    fs_type
        Expected filesystem, following the parted names.

    start
        Start of the partition (in parted units)

    end
        End of the partition (in parted units)

    flags
        List of flags present in the partition

    '''
    ret = {
        'name': name,
        'result': False,
        'changes': {},
        'comment': [],
    }

    if part_type not in ('primary', 'extended', 'logical'):
        ret['comment'].append('Partition type not recognized')
    if not start or not end:
        ret['comment'].append('Parameters start and end are not optional')

    # Normalize fs_type. Some versions of salt contains a bug were
    # only a subset of file systems are valid for mkpart, even if are
    # supported by parted. As mkpart do not format the partition, is
    # safe to make a normalization here. Eventually this is only used
    # to set the type in the flag section (partition id).
    #
    # We can drop this check in the next version of salt.
    if fs_type and fs_type not in set(['ext2', 'fat32', 'fat16',
                                       'linux-swap', 'reiserfs',
                                       'hfs', 'hfs+', 'hfsx', 'NTFS',
                                       'ufs', 'xfs', 'zfs']):
        fs_type = 'ext2'

    flags = flags if flags else []

    # If the user do not provide any partition number we get generate
    # the next available for the partition type
    device_md, device_no_md, number = re.search(
        r'(?:(/dev/md[^p]+)p?|(\D+))(\d*)', name).groups()
    device = device_md if device_md else device_no_md
    if not number:
        try:
            number = _get_partition_number(device, part_type, start, end)
        except EnumerateException as e:
            ret['comment'].append(str(e))

    # If at this point we have some comments, we return with a fail
    if ret['comment']:
        return ret

    # Check if the partition is already there or we need to create a
    # new one
    partition_match = _check_partition(device, number, part_type,
                                       start, end)

    if partition_match:
        ret['result'] = True
        ret['comment'].append('Partition {}{} already '
                              'in place'.format(device, number))
        return ret
    elif partition_match is None:
        ret['changes']['new'] = 'Partition {}{} will ' \
            'be created'.format(device, number)
    elif partition_match is False:
        ret['comment'].append('Partition {}{} cannot '
                              'be replaced'.format(device, number))
        return ret

    if __opts__['test']:
        ret['result'] = None
        return ret

    if partition_match is None:
        # TODO(aplanas) with parted we cannot force a partition number
        res = __salt__['partition.mkpart'](device, part_type, fs_type,
                                           start, end)
        ret['changes']['output'] = res

        # Wipe the filesystem information from the partition to remove
        # old data that was on the disk.  As a side effect, this will
        # force the mkfs state to happend.
        __salt__['disk.wipe']('{}{}'.format(device, number))

        _invalidate_cached_info()
        _invalidate_cached_partitions()

    # The first time that we create a partition we do not have a
    # partition number for it
    if not number:
        number = _get_partition_number(device, part_type, start, end)

    partition_match = _check_partition(device, number, part_type,
                                       start, end)
    if partition_match:
        ret['result'] = True
    elif not partition_match:
        ret['comment'].append('Partition {}{} fail to '
                              'be created'.format(device, number))
        ret['result'] = False

    # We set the correct flags for the partition
    current_flags = _get_partition_flags(device, number)
    flags_to_set = set(flags) - set(current_flags)
    flags_to_unset = set(current_flags) - set(flags)

    for flag in flags_to_set:
        try:
            out = __salt__['partition.set'](device, number, flag, 'on')
        except CommandExecutionError as e:
            out = e
        if out:
            ret['comment'].append('Error setting flag {} in {}{}: {}'
                                  .format(flag, device, number, out))
            ret['result'] = False
        else:
            ret['changes'][flag] = True

    for flag in flags_to_unset:
        try:
            out = __salt__['partition.set'](device, number, flag, 'off')
        except CommandExecutionError as e:
            out = e
        if out:
            ret['comment'].append('Error unsetting flag {} in {}{}: {}'
                                  .format(flag, device, number, out))
            ret['result'] = False
        else:
            ret['changes'][flag] = False

    return ret


def _check_partition_name(device, number, name):
    '''
    Check if the partition have this name.

    Returns a tri-state value:
      - `True`: the partition already have this label
      - `False`: the partition do not have this label
      - `None`: there is not such partition
    '''
    number = str(number)
    partitions = _get_cached_partitions(device)
    if number in partitions:
        return partitions[number]['name'] == name


def named(name, device, partition=None):
    '''
    Make sure that a gpt partition have set a name.

    name
        Name or label for the partition

    device
        Device name (/dev/sda, /dev/disk/by-id/scsi-...) or partition

    partition
        Partition number (can be in the device)

    '''
    ret = {
        'name': name,
        'result': False,
        'changes': {},
        'comment': [],
    }

    if not partition:
        device, partition = re.search(r'(\D+)(\d*)', device).groups()
    if not partition:
        ret['comment'].append('Partition number not provided')

    if not _check_label(device, 'gpt'):
        ret['comment'].append('Only gpt partitions can be named')

    name_match = _check_partition_name(device, partition, name)
    if name_match:
        ret['result'] = True
        ret['comment'].append('Name of the partition {}{} is '
                              'already "{}"'.format(device, partition, name))
    elif name_match is None:
        ret['comment'].append('Partition {}{} not found'.format(device,
                                                                partition))

    if ret['comment']:
        return ret

    if __opts__['test']:
        ret['comment'].append('Partition {}{} will be named '
                              '"{}"'.format(device, partition, name))
        ret['changes']['name'] = 'Name will be set to {}'.format(name)
        return ret

    changes = __salt__['partition.name'](device, partition, name)
    _invalidate_cached_info()
    _invalidate_cached_partitions()

    if _check_partition_name(device, partition, name):
        ret['result'] = True
        ret['comment'].append('Name set to {} in {}{}'.format(name, device,
                                                              partition))
        ret['changes']['name'] = changes
    else:
        ret['comment'].append('Failed to set name to {}'.format(name))

    return ret


def _check_disk_flags(device, flag):
    '''
    Return True if the flag for a device is already set.
    '''
    flags = __salt__['partition.list'](device)['info']['disk flags']
    return flag in flags


def disk_set(name, flag, enabled=True):
    '''
    Make sure that a disk flag is set or unset.

    name
        Device name (/dev/sda, /dev/disk/by-id/scsi-...)

    flag
        A valid parted disk flag (see ``parted.disk_set``)

    enabled
        Boolean value

    CLI Example:

    .. code-block:: bash

        salt '*' partitioned.disk_set /dev/sda pmbr_boot
        salt '*' partitioned.disk_set /dev/sda pmbr_boot False

    '''
    ret = {
        'name': name,
        'result': False,
        'changes': {},
        'comment': [],
    }

    is_flag = _check_disk_flags(name, flag)
    if enabled == is_flag:
        ret['result'] = True
        ret['comment'].append('Flag {} in {} already {}'
                              .format(flag, name,
                                      'set' if enabled else 'unset'))
        return ret

    if __opts__['test']:
        ret['comment'].append('Flag {} in {} will be {}'
                              .format(flag, name,
                                      'set' if enabled else 'unset'))
        ret['changes'][flag] = enabled
        return ret

    __salt__['partition.disk_set'](name, flag, 'on' if enabled else 'off')

    is_flag = _check_disk_flags(name, flag)
    if enabled == is_flag:
        ret['result'] = True
        ret['comment'].append('Flag {} {} in {}'
                              .format(flag,
                                      'set' if enabled else 'unset', name))
        ret['changes'][flag] = enabled
    else:
        ret['comment'].append('Failed to {} {} in {}'
                              .format('set' if enabled else 'unset', flag,
                                      name))

    return ret


def _check_partition_flags(device, number, flag):
    '''
    Return True if the flag for a partition is already set.

    Returns a tri-state value:
      - `True`: the partition already have this flag
      - `False`: the partition do not have this flag
      - `None`: there is not such partition
    '''
    number = str(number)
    partitions = __salt__['partition.list'](device)['partitions']
    if number in partitions:
        return flag in partitions[number]['flags']


def partition_set(name, flag, partition=None, enabled=True):
    '''
    Make sure that a partition flag is set or unset.

    name
        Device name (/dev/sda, /dev/disk/by-id/scsi-...) or partition

    flag
        A valid parted disk flag (see ``parted.disk_set``)

    partition
        Partition number (can be in the device name)

    enabled
        Boolean value

    CLI Example:

    .. code-block:: bash

        salt '*' partitioned.partition_set /dev/sda1 bios_grub
        salt '*' partitioned.partition_set /dev/sda bios_grub 1 False

    '''
    ret = {
        'name': name,
        'result': False,
        'changes': {},
        'comment': [],
    }

    if not partition:
        name, partition = re.search(r'(\D+)(\d*)', name).groups()
    if not partition:
        ret['comment'].append('Partition number not provided')

    is_flag = _check_partition_flags(name, partition, flag)
    if enabled == is_flag:
        ret['result'] = True
        ret['comment'].append('Flag {} in {}{} already {}'
                              .format(flag, name, partition,
                                      'set' if enabled else 'unset'))
    elif is_flag is None:
        ret['comment'].append('Partition {}{} not found'
                              .format(name, partition))

    if ret['comment']:
        return ret

    if __opts__['test']:
        ret['comment'].append('Flag {} in {}{} will be {}'
                              .format(flag, name, partition,
                                      'set' if enabled else 'unset'))
        ret['changes'][flag] = enabled
        return ret

    __salt__['partition.set'](name, partition, flag,
                              'on' if enabled else 'off')

    is_flag = _check_partition_flags(name, partition, flag)
    if enabled == is_flag:
        ret['result'] = True
        ret['comment'].append('Flag {} {} in {}{}'
                              .format(flag, 'set' if enabled else 'unset',
                                      name, partition))
        ret['changes'][flag] = enabled
    else:
        ret['comment'].append('Failed to {} {} in {}{}'
                              .format('set' if enabled else 'unset', flag,
                                      name, partition))
    return ret
