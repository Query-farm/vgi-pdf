"""In-process VGI invocation helpers for the calendar worker test suite.

Drives a table function through the real bind -> init -> process lifecycle
without spawning a worker process, so most tests stay fast and debuggable.
Adapted from the vgi-scikit-learn worker test suite.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
from vgi.arguments import Arguments
from vgi.function_storage import BoundStorage, FunctionStorage, FunctionStorageSqlite
from vgi.invocation import FunctionType
from vgi.protocol import BindRequest, InitRequest
from vgi.table_function import ProcessParams


def test_storage() -> FunctionStorage:
    """Real in-memory FunctionStorage for the function lifecycle in tests."""
    return FunctionStorageSqlite(":memory:")


class MockOutputCollector:
    """Captures emitted batches for assertions."""

    def __init__(self, output_schema: pa.Schema) -> None:
        self.output_schema = output_schema
        self.batches: list[pa.RecordBatch] = []
        self._finished = False

    def emit(
        self,
        batch: pa.RecordBatch,
        partition_values: dict[str, Any] | None = None,
        metadata: dict[str, str] | None = None,
    ) -> None:
        self.batches.append(batch)

    def finish(self) -> None:
        self._finished = True

    @property
    def finished(self) -> bool:
        return self._finished

    def emit_client_log_message(self, msg: Any) -> None:
        pass


def invoke_table_function(
    func_cls: type,
    *,
    named: dict[str, pa.Scalar] | None = None,
    positional: tuple[pa.Scalar, ...] = (),
    serialize_state: bool = False,
) -> pa.Table:
    """Run a (source) table function through bind -> init -> process -> table.

    When ``serialize_state`` is True, the scan state is round-tripped through its
    Arrow serialization between every ``process`` tick -- mimicking the stateless
    HTTP transport, which wire-serializes the continuation state after each tick
    and resumes by deserializing it. This proves the cursor survives batch
    boundaries (the old emit-all + ``state: None`` code loops forever here). A
    1000-tick guard turns an infinite loop into a clean failure instead of a hang.
    """
    args = Arguments(positional=positional, named=named or {})

    bind_req = BindRequest(
        function_name=func_cls.Meta.name,
        arguments=args,
        function_type=FunctionType.TABLE,
    )
    bind_resp = func_cls.bind(bind_req)

    init_req = InitRequest(bind_call=bind_req, output_schema=bind_resp.output_schema)
    init_resp = func_cls.global_init(init_req)

    storage = test_storage()
    params = ProcessParams(
        args=func_cls._parse_arguments(func_cls.FunctionArguments, args),
        init_call=init_req,
        init_response=init_resp,
        output_schema=bind_resp.output_schema,
        settings={},
        secrets={},
        storage=BoundStorage(storage, init_resp.execution_id),
    )

    state = func_cls.initial_state(params)
    state_type = type(state) if state is not None else None
    out = MockOutputCollector(bind_resp.output_schema)

    guard = 0
    while not out.finished:
        guard += 1
        if guard > 1000:
            raise AssertionError("process did not finish within 1000 ticks")
        func_cls.process(params, state, out)
        if serialize_state and state is not None and state_type is not None:
            # Round-trip the state exactly as the HTTP transport would per tick.
            state = state_type.deserialize_from_bytes(state.serialize_to_bytes())

    return pa.Table.from_batches(out.batches, schema=bind_resp.output_schema)
