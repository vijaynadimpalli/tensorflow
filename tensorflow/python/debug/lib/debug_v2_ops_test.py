# Copyright 2019 The TensorFlow Authors. All Rights Reserved.
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
# ==============================================================================
"""Test for the internal ops used by tfdbg v2."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os

import numpy as np

from tensorflow.core.protobuf import debug_event_pb2
from tensorflow.python.debug.lib import debug_events_reader
from tensorflow.python.debug.lib import debug_events_writer
from tensorflow.python.debug.lib import dumping_callback_test_lib
from tensorflow.python.eager import def_function
from tensorflow.python.framework import constant_op
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import errors
from tensorflow.python.framework import ops
from tensorflow.python.framework import tensor_util
from tensorflow.python.framework import test_util
from tensorflow.python.ops import gen_debug_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.platform import googletest


class DebugIdentityV2OpTest(dumping_callback_test_lib.DumpingCallbackTestBase):

  def setUp(self):
    super(DebugIdentityV2OpTest, self).setUp()
    # Testing using a small circular-buffer size.
    self.circular_buffer_size = 4
    self.writer = debug_events_writer.DebugEventsWriter(
        self.dump_root, self.circular_buffer_size)

  def tearDown(self):
    self.writer.Close()
    super(DebugIdentityV2OpTest, self).tearDown()

  @test_util.run_in_graph_and_eager_modes
  def testSingleTensorFullTensorDebugModeWithCircularBufferBehavior(self):

    @def_function.function
    def write_debug_trace(x):
      # DebugIdentityV2 is a stateful op. It ought to be included by auto
      # control dependency.
      square = math_ops.square(x)
      gen_debug_ops.debug_identity_v2(
          square,
          tfdbg_context_id="deadbeaf",
          op_name="Square",
          output_slot=0,
          tensor_debug_mode=debug_event_pb2.TensorDebugMode.FULL_TENSOR,
          debug_urls=["file://%s" % self.dump_root])

      sqrt = math_ops.sqrt(x)
      gen_debug_ops.debug_identity_v2(
          sqrt,
          tfdbg_context_id="beafdead",
          op_name="Sqrt",
          output_slot=0,
          tensor_debug_mode=debug_event_pb2.TensorDebugMode.FULL_TENSOR,
          debug_urls=["file://%s" % self.dump_root])
      return square + sqrt

    x = np.array([3.0, 4.0])
    # Only the graph-execution trace of the last iteration should be written
    # to self.dump_root.
    for _ in range(self.circular_buffer_size // 2 + 1):
      self.assertAllClose(
          write_debug_trace(x), [9.0 + np.sqrt(3.0), 16.0 + 2.0])

    with debug_events_reader.DebugEventsReader(self.dump_root) as reader:
      metadata_iter = reader.metadata_iterator()
      # Check that the .metadata DebugEvents data file has been created, even
      # before FlushExecutionFiles() is called.
      debug_event = next(metadata_iter)
      self.assertGreater(debug_event.wall_time, 0)
      self.assertTrue(debug_event.debug_metadata.tensorflow_version)
      self.assertTrue(
          debug_event.debug_metadata.file_version.startswith("debug.Event:"))

      graph_trace_iter = reader.graph_execution_traces_iterator()
      # Before FlushExecutionFiles() is called, the .graph_execution_traces file
      # ought to be empty.
      with self.assertRaises(StopIteration):
        next(graph_trace_iter)

      # Flush the circular buffer.
      self.writer.FlushExecutionFiles()
      graph_trace_iter = reader.graph_execution_traces_iterator()

      # The circular buffer has a size of 4. So only the data from the
      # last two iterations should have been written to self.dump_root.
      for _ in range(2):
        debug_event = next(graph_trace_iter)
        self.assertGreater(debug_event.wall_time, 0)
        trace = debug_event.graph_execution_trace
        self.assertEqual(trace.tfdbg_context_id, "deadbeaf")
        self.assertEqual(trace.op_name, "Square")
        self.assertEqual(trace.output_slot, 0)
        self.assertEqual(trace.tensor_debug_mode,
                         debug_event_pb2.TensorDebugMode.FULL_TENSOR)
        tensor_value = tensor_util.MakeNdarray(trace.tensor_proto)
        self.assertAllClose(tensor_value, [9.0, 16.0])

        debug_event = next(graph_trace_iter)
        self.assertGreater(debug_event.wall_time, 0)
        trace = debug_event.graph_execution_trace
        self.assertEqual(trace.tfdbg_context_id, "beafdead")
        self.assertEqual(trace.op_name, "Sqrt")
        self.assertEqual(trace.output_slot, 0)
        self.assertEqual(trace.tensor_debug_mode,
                         debug_event_pb2.TensorDebugMode.FULL_TENSOR)
        tensor_value = tensor_util.MakeNdarray(trace.tensor_proto)
        self.assertAllClose(tensor_value, [np.sqrt(3.0), 2.0])

      # Only the graph-execution trace of the last iteration should be written
      # to self.dump_root.
      with self.assertRaises(StopIteration):
        next(graph_trace_iter)

  @test_util.run_in_graph_and_eager_modes
  def testControlFlow(self):

    @def_function.function
    def collatz(x):
      counter = constant_op.constant(0, dtype=dtypes.int32)
      while math_ops.greater(x, 1):
        counter = counter + 1
        gen_debug_ops.debug_identity_v2(
            x,
            tfdbg_context_id="deadbeaf",
            op_name="x",
            output_slot=0,
            tensor_debug_mode=debug_event_pb2.TensorDebugMode.FULL_TENSOR,
            debug_urls=["file://%s" % self.dump_root])
        if math_ops.equal(x % 2, 0):
          x = math_ops.div(x, 2)
        else:
          x = x * 3 + 1
      return counter

    x = constant_op.constant(10, dtype=dtypes.int32)
    self.evaluate(collatz(x))

    self.writer.FlushExecutionFiles()
    with debug_events_reader.DebugEventsReader(self.dump_root) as reader:
      graph_trace_iter = reader.graph_execution_traces_iterator()
      try:
        x_values = []
        timestamp = 0
        while True:
          debug_event = next(graph_trace_iter)
          self.assertGreater(debug_event.wall_time, timestamp)
          timestamp = debug_event.wall_time
          trace = debug_event.graph_execution_trace
          self.assertEqual(trace.tfdbg_context_id, "deadbeaf")
          self.assertEqual(trace.op_name, "x")
          self.assertEqual(trace.output_slot, 0)
          self.assertEqual(trace.tensor_debug_mode,
                           debug_event_pb2.TensorDebugMode.FULL_TENSOR)
          x_values.append(int(tensor_util.MakeNdarray(trace.tensor_proto)))
      except StopIteration:
        pass

      # Due to the circular buffer, only the last 4 iterations of
      # [10, 5, 16, 8, 4, 2] should have been written.
      self.assertAllEqual(x_values, [16, 8, 4, 2])

  @test_util.run_in_graph_and_eager_modes
  def testTwoDumpRoots(self):
    another_dump_root = os.path.join(self.dump_root, "another")
    another_debug_url = "file://%s" % another_dump_root
    another_writer = debug_events_writer.DebugEventsWriter(another_dump_root)

    @def_function.function
    def write_debug_trace(x):
      # DebugIdentityV2 is a stateful op. It ought to be included by auto
      # control dependency.
      square = math_ops.square(x)
      gen_debug_ops.debug_identity_v2(
          square,
          tfdbg_context_id="deadbeaf",
          tensor_debug_mode=debug_event_pb2.TensorDebugMode.FULL_TENSOR,
          debug_urls=["file://%s" % self.dump_root, another_debug_url])
      return square + 1.0

    x = np.array([3.0, 4.0])
    self.assertAllClose(write_debug_trace(x), np.array([10.0, 17.0]))
    self.writer.FlushExecutionFiles()
    another_writer.FlushExecutionFiles()
    another_writer.Close()

    for debug_root in (self.dump_root, another_dump_root):
      with debug_events_reader.DebugEventsReader(debug_root) as reader:
        graph_trace_iter = reader.graph_execution_traces_iterator()

        debug_event = next(graph_trace_iter)
        trace = debug_event.graph_execution_trace
        self.assertEqual(trace.tfdbg_context_id, "deadbeaf")
        self.assertEqual(trace.op_name, "")
        self.assertEqual(trace.tensor_debug_mode,
                         debug_event_pb2.TensorDebugMode.FULL_TENSOR)
        tensor_value = tensor_util.MakeNdarray(trace.tensor_proto)
        self.assertAllClose(tensor_value, [9.0, 16.0])

        with self.assertRaises(StopIteration):
          next(graph_trace_iter)

  @test_util.run_in_graph_and_eager_modes
  def testDebugNumericSummaryV2OpReduceInfNanThreeSlots(self):

    def debug_summary(x):
      return self.evaluate(gen_debug_ops.debug_numeric_summary_v2(
          x, tensor_debug_mode=(
              debug_event_pb2.TensorDebugMode.REDUCE_INF_NAN_THREE_SLOTS)))

    self.assertAllEqual(
        debug_summary(constant_op.constant([])), [0.0, 0.0, 0.0])
    self.assertAllEqual(
        debug_summary(constant_op.constant(42.0)), [0.0, 0.0, 0.0])
    self.assertAllEqual(
        debug_summary(constant_op.constant([3.0, 4.0])), [0.0, 0.0, 0.0])
    self.assertAllEqual(
        debug_summary(constant_op.constant(np.array([3.0, -np.inf]))),
        [-np.inf, 0.0, 0.0])
    self.assertAllEqual(
        debug_summary(constant_op.constant(np.array([[0, 0], [np.nan, 0]]))),
        [0.0, 0.0, np.nan])
    self.assertAllEqual(
        debug_summary(
            constant_op.constant(np.array([[0, 0], [np.nan, np.inf]]))),
        [0.0, np.inf, np.nan])
    self.assertAllEqual(
        debug_summary(
            constant_op.constant(np.array([[0, np.inf], [np.nan, -np.inf]]))),
        [-np.inf, np.inf, np.nan])

    x = np.zeros([100, 100], dtype=np.float16)
    x[32, 47] = np.nan
    self.assertAllEqual(
        debug_summary(constant_op.constant(x)), [0.0, 0.0, np.nan])
    x = np.zeros([97, 97], dtype=np.float32)
    x[50, 83] = -np.inf
    self.assertAllEqual(
        debug_summary(constant_op.constant(x)), [-np.inf, 0.0, 0.0])
    x[1, 41] = np.nan
    self.assertAllEqual(
        debug_summary(constant_op.constant(x)), [-np.inf, 0.0, np.nan])
    x = np.zeros([9701], dtype=np.float64)
    x[9700] = np.nan
    self.assertAllEqual(
        debug_summary(constant_op.constant(x)), [0.0, 0.0, np.nan])

  @test_util.run_in_graph_and_eager_modes
  def testDebugNumericSummaryV2OpLargeTensorIDError(self):
    modes = [
        debug_event_pb2.TensorDebugMode.CURT_HEALTH,
    ]
    # Maximum allowed tensor_id
    tensor_id = np.power(2, 53)
    for mode in modes:
      self.evaluate(
          gen_debug_ops.debug_numeric_summary_v2(
              constant_op.constant(42.0),
              tensor_debug_mode=mode,
              tensor_id=tensor_id,
              output_dtype=dtypes.float64))
    # Incrementing by one should error
    tensor_id += 1
    for mode in modes:
      with self.assertRaises(errors.InvalidArgumentError):
        self.evaluate(
            gen_debug_ops.debug_numeric_summary_v2(
                constant_op.constant(42.0),
                tensor_debug_mode=mode,
                tensor_id=tensor_id,
                output_dtype=dtypes.float64))

  @test_util.run_in_graph_and_eager_modes
  def testDebugNumericSummaryV2OpCurtHealthValuesSmall(self):

    def debug_summary(x):
      return self.evaluate(
          gen_debug_ops.debug_numeric_summary_v2(
              x,
              tensor_debug_mode=(debug_event_pb2.TensorDebugMode.CURT_HEALTH),
              tensor_id=x._id,
              output_dtype=dtypes.float64)), x._id

    tensor, tensor_id = debug_summary(constant_op.constant([]))
    self.assertAllEqual(tensor, [tensor_id, 0.0])

    tensor, tensor_id = debug_summary(constant_op.constant(42.0))
    self.assertAllEqual(tensor, [tensor_id, 0.0])

    tensor, tensor_id = debug_summary(constant_op.constant([3.0, 4.0]))
    self.assertAllEqual(tensor, [tensor_id, 0.0])

    tensor, tensor_id = debug_summary(
        constant_op.constant(np.array([3.0, -np.inf])))
    self.assertAllEqual(tensor, [tensor_id, 1.0])

    tensor, tensor_id = debug_summary(
        constant_op.constant(np.array([[0, 0], [np.nan, 0]])))
    self.assertAllEqual(tensor, [tensor_id, 1.0])

    tensor, tensor_id = debug_summary(
        constant_op.constant(np.array([[0, 0], [np.nan, np.inf]])))
    self.assertAllEqual(tensor, [tensor_id, 1.0])

    tensor, tensor_id = debug_summary(
        constant_op.constant(np.array([[0, np.inf], [np.nan, -np.inf]])))
    self.assertAllEqual(tensor, [tensor_id, 1.0])

  @test_util.run_in_graph_and_eager_modes
  def testDebugNumericSummaryV2OpCurtHealthValuesLarge(self):

    def debug_summary(x):
      return self.evaluate(
          gen_debug_ops.debug_numeric_summary_v2(
              x,
              tensor_debug_mode=(debug_event_pb2.TensorDebugMode.CURT_HEALTH),
              tensor_id=x._id,
              output_dtype=dtypes.float64)), x._id

    x = np.zeros([100, 100], dtype=np.float16)
    x[32, 47] = np.nan
    tensor, tensor_id = debug_summary(constant_op.constant(x))
    self.assertAllEqual(tensor, [tensor_id, 1.0])
    x = np.zeros([97, 97], dtype=np.float32)
    x[50, 83] = -np.inf
    tensor, tensor_id = debug_summary(constant_op.constant(x))
    self.assertAllEqual(tensor, [tensor_id, 1.0])
    x[1, 41] = np.nan
    tensor, tensor_id = debug_summary(constant_op.constant(x))
    self.assertAllEqual(tensor, [tensor_id, 1.0])
    x = np.zeros([9701], dtype=np.float64)
    x[9700] = np.nan
    tensor, tensor_id = debug_summary(constant_op.constant(x))
    self.assertAllEqual(tensor, [tensor_id, 1.0])

  @test_util.run_in_graph_and_eager_modes
  def testDebugNumericSummaryV2OpCurtHealthConsistency(self):

    def debug_summary(x):
      return self.evaluate(
          gen_debug_ops.debug_numeric_summary_v2(
              x,
              tensor_debug_mode=(debug_event_pb2.TensorDebugMode.CURT_HEALTH),
              tensor_id=x._id,
              output_dtype=dtypes.float64)), x._id

    x = np.zeros([100, 100], dtype=np.float16)
    x[43, 99] = np.nan
    c = constant_op.constant(x)
    tensor_1, tensor_id_1 = debug_summary(c)
    tensor_2, tensor_id_2 = debug_summary(c)
    self.assertAllEqual(tensor_1, tensor_2)
    self.assertEqual(tensor_id_1, tensor_id_2)

    x = np.zeros([100, 100, 50], dtype=np.float64)
    x[0, 0, 1] = np.inf
    c = constant_op.constant(x)
    tensor_1, tensor_id_1 = debug_summary(c)
    tensor_2, tensor_id_2 = debug_summary(c)
    self.assertAllEqual(tensor_1, tensor_2)
    self.assertEqual(tensor_id_1, tensor_id_2)

    c = constant_op.constant(np.ones((100, 200), np.double))
    tensor_1, tensor_id_1 = debug_summary(c)
    tensor_2, tensor_id_2 = debug_summary(c)
    self.assertAllEqual(tensor_1, tensor_2)
    self.assertEqual(tensor_id_1, tensor_id_2)


if __name__ == "__main__":
  ops.enable_eager_execution()
  googletest.main()
