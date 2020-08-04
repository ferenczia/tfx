# Lint as: python3
# Copyright 2020 Google LLC. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Helper utils for executor verifier."""

import os

import absl
from typing import Dict, List, Text, Optional

import tensorflow as tf
import tensorflow_model_analysis as tfma
from tensorflow_metadata.proto.v0 import anomalies_pb2
from tensorflow_model_analysis.view import SlicedMetrics
from ml_metadata.proto import metadata_store_pb2

from tfx import types
from tfx.orchestration import data_types
from tfx.orchestration import metadata
from tfx.utils import io_utils

def _compare_relative_difference(value: float,
                                 expected_value: float,
                                 threshold: float) -> bool:
  """Returns whether relative difference between value and expected_value
    is within a specified threshold.

  Args:
    value: a float value to be compared to expected value.
    expected_value: a float value that is expected.
    threshold: a float between 0 and 1.

  Returns:
    a boolean indicating whether the relative difference is within the
    threshold
  """
  print("expected_value", expected_value)
  print("value", value)
  if value != expected_value:
    if expected_value:
      relative_diff = abs(value - expected_value)/abs(expected_value)
      if not (expected_value and relative_diff <= threshold):
        absl.logging.warning(
            "Relative difference {} exceeded threshold {}"\
                                        .format(relative_diff, threshold))
        return False
  return True

def get_pipeline_outputs(
    metadata_connection_config:
        Optional[metadata_store_pb2.ConnectionConfig],
    pipeline_info: data_types.PipelineInfo
    ) -> Dict[Text, Dict[Text, List[types.Artifact]]]:
  """Returns a dictionary of pipeline output artifacts for every component.

  Args:
    metadata_connection_config: connection configuration to MLMD.
    pipeline_info: pipeline info from orchestration.

  Returns:
    a dictionary of holding list of artifacts for a component id.
  """
  output_map = {}
  with metadata.Metadata(metadata_connection_config) as m:
    context = m.get_pipeline_run_context(pipeline_info)
    for execution in m.store.get_executions_by_context(context.id):
      component_id = execution.properties['component_id'].string_value
      output_dict = {}
      for event in m.store.get_events_by_execution_ids([execution.id]):
        if event.type == metadata_store_pb2.Event.OUTPUT:
          artifacts = m.store.get_artifacts_by_id([event.artifact_id])
          steps = event.path.steps
          if not steps or not steps[0].HasField('key'):
            raise ValueError('Artifact key is not recorded in the MLMD.')
          key = steps[0].key
          artifacts = m.store.get_artifacts_by_id(
              [event.artifact_id])
          if key not in output_dict:
            output_dict[key] = {}
          for artifact in artifacts:
            if len(steps) < 2 or not steps[1].HasField('index'):
              raise ValueError('Artifact index is not recorded in the MLMD.')
            artifact_index = steps[1].index
            if artifact_index in output_dict[key]:
              raise ValueError("Artifact already in output_dict")
            output_dict[key][artifact_index] = artifact
      output_map[component_id] = output_dict
  return output_map

def verify_file_dir(output_uri: Text,
                    expected_uri: Text,
                    check_file: bool = False):
  """Verify pipeline output artifact uri by comparing directory structure.

  Args:
    output_uri: pipeline output artifact uri.
    expected_uri: recorded pipeline output artifact uri.
    check_file: boolean indicating whether to check file path.

  Returns:
    a boolean whether file paths are matching.
  """
  for dir_name, sub_dirs, leaf_files in tf.io.gfile.walk(expected_uri):
    for sub_dir in sub_dirs:
      new_file_path = os.path.join(
          dir_name.replace(expected_uri, output_uri, 1), sub_dir)
      if not tf.io.gfile.exists(new_file_path):
        return False
    if check_file:
      for leaf_file in leaf_files:
        new_file_path = os.path.join(
            dir_name.replace(expected_uri, output_uri, 1), leaf_file)
        if not tf.io.gfile.exists(new_file_path):
          return False
  return True

def _group_metric_by_slice(eval_result_metric: List[SlicedMetrics]
                          ) -> Dict[Text, Dict[Text, float]]:
  """Returns a dictionary holding metric values for every slice.

  Args:
    eval_result_metric: list of sliced metrics.

  Returns:
    a slice map that holds a dictionary of metric and value for slices
  """
  slice_map = {}
  for metric in eval_result_metric:
    slice_map[metric[0]] = {k: v['doubleValue'] \
                              for k, v in metric[1][''][''].items()}
  return slice_map

def compare_eval_results(output_uri: Text,
                         expected_uri: Text,
                         threshold: float) -> bool:
  """Compares accuracy on overall dataset using two EvalResult.

  Args:
    eval_result: Result of a model analysis run.
    expected_eval_result: Result of a model analysis run.
    threshold: a float between 0 and 1.

  Returns:
    boolean whether the eval result values differ within a threshold.
  """
  eval_result = tfma.load_eval_result(output_uri)
  expected_eval_result = tfma.load_eval_result(expected_uri)
  eval_slicing_metrics = eval_result.slicing_metrics
  expected_slicing_metrics = expected_eval_result.slicing_metrics
  slice_map = _group_metric_by_slice(eval_slicing_metrics)
  expected_slice_map = _group_metric_by_slice(expected_slicing_metrics)
  for metric_name, value in slice_map[()].items():
    expected_value = expected_slice_map[()][metric_name]
    if not _compare_relative_difference(value, expected_value, threshold):
      print("expected_value", expected_value)
      print("value", value)
      print("metric_name", metric_name)
      return False
  return True

def compare_file_sizes(output_uri: Text,
                       expected_uri: Text,
                       threshold: float) -> bool:
  """Compares pipeline output files sizes in output and recorded uri.

  Args:
    output_uri: pipeline output artifact uri.
    expected_uri: recorded pipeline output artifact uri.
    threshold: a float between 0 and 1.

  Returns:
     boolean whether file sizes differ within a threshold.
  """
  for dir_name, sub_dirs, leaf_files in tf.io.gfile.walk(expected_uri):
    for sub_dir in sub_dirs:
      new_file_path = os.path.join(
          dir_name.replace(expected_uri, output_uri, 1), sub_dir)
      if not tf.io.gfile.exists(new_file_path):
        return False
    for leaf_file in leaf_files:
      expected_file_name = os.path.join(dir_name, leaf_file)
      file_name = os.path.join(
          dir_name.replace(expected_uri, output_uri, 1), leaf_file)
      if not _compare_relative_difference(
          tf.io.gfile.GFile(file_name).size(),
          tf.io.gfile.GFile(expected_file_name).size(),
          threshold):
        return False
  return True

def compare_model_file_sizes(output_uri: Text,
                       expected_uri: Text,
                       threshold: float) -> bool:
  """Compares pipeline output files sizes in output and recorded uri.

  Args:
    output_uri: pipeline output artifact uri.
    expected_uri: recorded pipeline output artifact uri.
    threshold: a float between 0 and 1.

  Returns:
     boolean whether file sizes differ within a threshold.
  """
  for dir_name, sub_dirs, leaf_files in tf.io.gfile.walk(expected_uri):
    if 'eval_model_dir' in dir_name or 'export' in dir_name:
      continue
    for sub_dir in sub_dirs:
      new_file_path = os.path.join(
          dir_name.replace(expected_uri, output_uri, 1), sub_dir)
      if not tf.io.gfile.exists(new_file_path):
        return False
    for leaf_file in leaf_files:
      if leaf_file.startswith('events.out.tfevents'):
        continue
      expected_file_name = os.path.join(dir_name, leaf_file)
      file_name = os.path.join(
          dir_name.replace(expected_uri, output_uri, 1), leaf_file)
      if not _compare_relative_difference(
          tf.io.gfile.GFile(file_name).size(),
          tf.io.gfile.GFile(expected_file_name).size(),
          threshold):
        return False
  return True

def compare_anomalies(output_uri: Text,
                      expected_uri: Text) -> bool:
  """Compares anomalies files in output uri and recorded uri.

  Args:
    output_uri: pipeline output artifact uri.
    expected_uri: recorded pipeline output artifact uri.

  Returns:
     boolean whether anomalies are same.
  """
  for dir_name, sub_dirs, leaf_files in tf.io.gfile.walk(expected_uri):
    for leaf_file in leaf_files:
      expected_file_name = os.path.join(dir_name, leaf_file)
      file_name = os.path.join(
          dir_name.replace(expected_uri, output_uri, 1), leaf_file)
      anomalies = io_utils.parse_pbtxt_file(
      os.path.join(output_uri, file_name),
      anomalies_pb2.Anomalies())
      expected_anomalies = io_utils.parse_pbtxt_file(
          os.path.join(expected_uri, expected_file_name),
          anomalies_pb2.Anomalies())
      if expected_anomalies.anomaly_info != anomalies.anomaly_info:
        print("filename", file_name)
        print("expected_file_name", expected_file_name)
        return False
  return True

  anomalies_fn = tf.io.gfile.listdir(output_uri)[0]
  expected_anomalies_fn = tf.io.gfile.listdir(expected_uri)[0]
  
