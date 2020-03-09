# -*- coding: utf-8 -*-
# Description: python module to read data from local FPGAs
# Author: Sebastian Dressler, Luc Vlaming, Ashley Fraser
# SPDX-License-Identifier: MIT

import time

from bases.FrameworkServices.SimpleService import SimpleService

import psycopg2
import copy
import subprocess
import re
import os
import time
import threading
from functools import wraps

priority = 90000

DEFINITIONS = {
    'bytes': {
        'options': [None, 'Transfered data', 'MB/sec', 'fpga', 'fpga', 'line'],
        'lines': [['host_to_fpga_byte_count', 'sent to fpga', 'incremental', 1, 1024*1024],
                  ['fpga_to_host_byte_count', 'received from fpga', 'incremental', -1, 1024*1024]]
    },
    'jobs': {
        'options': [None, 'Processed jobs', 'Jobs/sec', 'fpga', 'fpga', 'line'],
        'lines': [['compression_job_count', 'compressed jobs', 'incremental'],
                  ['decompression_job_count', 'decompressed jobs', 'incremental'],
                  ['decompression_and_filter_job_count', 'decompressed and filtered jobs', 'incremental'],
                  ['filter_job_count', 'filtered jobs', 'incremental']
                 ]
    },
    'max': {
        'options': [None, 'Max outstanding jobs', 'max oustanding', 'fpga', 'fpga', 'line'],
        'lines': [['max_outstanding_compression_jobs', 'compression', 'absolute'],
                  ['max_outstanding_decompression_and_filter_jobs', 'decompress and filter', 'absolute'],
                  ['max_outstanding_filter_jobs', 'filter', 'absolute']
                 ]
    },
    'pu_stats': {
        'options': [None, 'PUs utilisation', 'PU utilised', 'fpga', 'fpga', 'line'],
        'lines': [['current_pu_utilised_comp_percent', 'current compress PUs (%)', 'absolute'],
                  ['current_pu_utilised_decomp_percent', 'current decompress PUs (%)', 'absolute'],
                  ['avg_pu_utilised_comp_percent', 'avg compress PUs (%)', 'absolute'],
                  ['avg_pu_utilised_decomp_percent', 'avg decompress PUs (%)', 'absolute'],
                  ['max_pu_utilised_comp', 'max. compress PUs', 'absolute'],
                  ['max_pu_utilised_decomp', 'max. decompress PUs', 'absolute']
                 ]
    },
    'ddr_stats': {
        'options': [None, 'Successful and denied DDR transfers', 'Transfers', 'fpga', 'fpga', 'line'],
        'lines': [['avg_memory_write_transactions_percent', 'successful write transfers (%)', 'absolute'],
                  ['avg_memory_read_transactions_percent', 'successful read transfers (%)', 'absolute'],
                  ['avg_memory_write_denied_percent', 'denied write transfers (%)', 'absolute'],
                  ['avg_memory_read_denied_percent', 'denied read transfers (%)', 'absolute']
                 ]
    },
    'temps': {
        'options': [None, 'FPGA Temperature', '°C', 'fpga', 'fpga', 'line'],
        'lines': [['temperature', 'Degrees Celcius', 'absolute']
                 ]
    },
    'powers': {
        'options': [None, 'FPGA Power Consumption', 'Watts', 'fpga', 'fpga', 'line'],
        'lines': [['power', 'Total Watts', 'absolute']
                 ]
    }
}

class Service(SimpleService):
    def __init__(self, configuration=None, name=None):
        SimpleService.__init__(self, configuration=configuration, name=name)
        self.conn = None
        self.default_data = dict()
        self.order = []
        self.definitions = dict()
        self.keys = []
        self.fpga_mapping = dict()
        self.next_fpga = 0
        self.intel_cmd = self.configuration.get('intel_cmd')
        self.xilinx_cmd = self.configuration.get('xilinx_cmd')
        self.fpga_count = 1
        self.dsn = self.configuration.get('dsn')
        self.check_temp_power = self.configuration.get('check_temp_power')
        self.pu_ddr_stats_enable = self.configuration.get('pu_ddr_stats_enable')
        self.temp = []
        self.power = []
        self.metrics = [ 'bytes', 'jobs', 'max' ]

        if os.path.exists(self.intel_cmd) and os.access(self.intel_cmd, os.X_OK):
            self.get_fpga_temp_func = self._get_intel_fpga_temp
            self.get_fpga_power_func = self._get_intel_fpga_power
        elif os.path.exists(self.xilinx_cmd) and os.access(self.xilinx_cmd, os.X_OK):
            self.get_fpga_temp_func = self._get_xilinx_fpga_temp
            self.get_fpga_power_func = self._get_xilinx_fpga_power

        self.temp_power_update_interval = 10 if self.update_every < 10 else self.update_every

        conn = self._connect(self.dsn)
        with conn.cursor() as cursor:
            cursor.execute('CREATE EXTENSION IF NOT EXISTS swarm64da')
            cursor.execute('SELECT COUNT(*) FROM swarm64da.get_fpga_stats()')
            self.fpga_count = cursor.fetchone()[0]

        if self.fpga_count > 1:
            for metric in self.metrics:
                self.init_fpga_metrics(metric, 'fpga-total')

        for i in range(self.fpga_count):
            self.temp.append(0)
            self.power.append(0)
            name = 'fpga-' + str(i)

            if self.pu_ddr_stats_enable:
                self.metrics.extend([ 'pu_stats', 'ddr_stats' ])

            if self.check_temp_power:
                self.metrics.extend([ 'temps', 'powers' ])

            for metric in self.metrics:
                self.init_fpga_metrics(metric, name)


        for key in self.keys:
            self.default_data[key] = 0

        for func in [ self._get_fpga_temp, self._get_fpga_power ]:
            thread = threading.Thread(target=func)
            thread.daemon = True
            thread.start()


    def init_fpga_metrics(self, component, name):
        component_name = name + '-' + component
        component_definition = DEFINITIONS[component]
        self.order.append(component_name)
        self.definitions[component_name] = copy.deepcopy(component_definition)
        self.definitions[component_name]['options'][3] = name

        for key in self.definitions[component_name]['lines']:
            key[0] = name + '-' + key[0]
            self.keys.append(key[0])


    @staticmethod
    def check():
        return True

    def _connect(self, dsn):
        if not self.conn:
            self.conn = psycopg2.connect(dsn)
            self.conn.autocommit = True

        return self.conn


    def _parse_intel_fpgainfo(self, cmd, re_string):
        try:
            fpga_info_res = subprocess.check_output(cmd, shell=True)
        except subprocess.CalledProcessError:
            return 0
        fpga_info_out = fpga_info_res.split(b'\n')

        for line in fpga_info_out:
            info_line = re.match(re_string, line.decode('UTF-8'))
            if info_line is not None:
                return info_line.group(1)


    def _parse_xilinx_fpgainfo(self, cmd, re_string):
        try:
            fpga_info_res = subprocess.check_output(cmd, shell=True)
        except subprocess.CalledProcessError:
            return 0
        fpga_info_out = fpga_info_res.split(b'\n')

        fpga_heading_found = False
        for line in fpga_info_out:
            info_line = re.match(re_string, line.decode('UTF-8'))
            value_line = re.match(r'^(\d+)\s+', line.decode('UTF-8'))
            if info_line is not None:
                fpga_heading_found = True
                continue
            if value_line is not None and fpga_heading_found is True:
                fpga_heading_found = False
                return value_line.group(0)


    def _get_intel_fpga_temp(self, idx):
        FPGA_TEMPERATURE_CMD = self.intel_cmd + " temp --device " + str(idx)
        RE_TEMP_STRING = r'^.*FPGA Core TEMP \s+: (\d+)'
        return self._parse_intel_fpgainfo(FPGA_TEMPERATURE_CMD, RE_TEMP_STRING)


    def _get_xilinx_fpga_temp(self, idx):
        FPGA_TEMPERATURE_CMD = self.xilinx_cmd + " query -d " + str(idx)
        RE_TEMP_STRING = r'FPGA TEMP'
        return self._parse_xilinx_fpgainfo(FPGA_TEMPERATURE_CMD, RE_TEMP_STRING)


    def _get_intel_fpga_power(self, idx):
        FPGA_POWER_CMD = self.intel_cmd + " power --device " + str(idx)
        RE_POWER_STRING = r'^.*Total Input Power \s+: (\d+)\.'
        return self._parse_intel_fpgainfo(FPGA_POWER_CMD, RE_POWER_STRING)


    def _get_xilinx_fpga_power(self, idx):
        FPGA_POWER_CMD = self.xilinx_cmd + " query -d " + str(idx)
        RE_POWER_STRING = r'Card Power'
        return self._parse_xilinx_fpgainfo(FPGA_POWER_CMD, RE_POWER_STRING)


    def time_lag(func):
        @wraps(func)
        def wrapper(self, *args, **kwargs):
            while True:
                before = time.time()
                func(self, *args, **kwargs)
                after = time.time()
                loop_time = self.temp_power_update_interval - (after - before)

                if loop_time >= 0:
                    time.sleep(loop_time)
                else:
                    time.sleep(self.temp_power_update_interval + loop_time)

        return wrapper


    @time_lag
    def _get_fpga_temp(self):
        for idx in range(self.fpga_count):
            self.temp[idx] = self.get_fpga_temp_func(idx)


    @time_lag
    def _get_fpga_power(self):
        for idx in range(self.fpga_count):
            self.power[idx] = self.get_fpga_power_func(idx)


    def set_fpga_os_status(self, data):
        for idx in range(self.fpga_count):
            temp_name = 'fpga-' + str(idx) + '-temperature'
            power_name = 'fpga-' + str(idx) + '-power'
            data[temp_name] = self.temp[idx]
            data[power_name] = self.power[idx]


    def get_data(self):
        # It might be that no connection could be established at all
        conn = self._connect(self.dsn)

        try:
            data = copy.deepcopy(self.default_data)
            if self.check_temp_power:
                self.set_fpga_os_status(data)


            with conn.cursor() as cursor:
                cursor.execute('CREATE EXTENSION IF NOT EXISTS swarm64da')
                cursor.execute('SELECT * FROM swarm64da.get_fpga_stats()')
                result = cursor.fetchall()
                columns = dict()

                idx = 0
                for column in cursor.description:
                    columns[column.name] = idx
                    idx += 1

                for row in result:
                    fpga_id = row[columns['fpga_id']] if 'fpga_id' in columns else '0'
                    if not fpga_id in self.fpga_mapping:
                        self.fpga_mapping[fpga_id] = 'fpga-' + str(self.next_fpga)
                        self.next_fpga += 1

                    fpga_key = self.fpga_mapping[fpga_id]

                    for column in cursor.description:
                        name = fpga_key + '-' + column.name
                        if self.fpga_count > 1:
                            tot_name = 'fpga-total-' + column.name
                        if name in self.keys:
                            data[name] = row[columns[column.name]]
			    if self.fpga_count > 1:
		                if 'percent' in name:
                                    data[tot_name] += row[columns[column.name]]/self.fpga_count
			        else:
                                    data[tot_name] += row[columns[column.name]]

                return data

        except Exception:
            self.conn = None
            raise
