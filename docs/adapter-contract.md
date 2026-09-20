# Adapter Contract

Every tool AATM invokes goes through an **adapter**. Adapters are the boundary
between the deterministic transaction engine and the (mock or real) outside
world. Compensation is also an adapter call — never a hidden direct function
call inside the coordinator.

## The protocol

An adapter must satisfy `aatm.adapters.base.ToolAdapter`:

```python
class ToolAdapter(Protocol):
    async def execute(self, intent: ActionIntent) -> ToolResult: ...
    async def query_status(self, intent_id: UUID) -> OutcomeQuery: ...
    async def verify_postcondition(
        self, intent: ActionIntent, state: dict[str, Any]
    ) -> VerificationResult: ...
```

- **`execute`** — perform the side effect for `intent` and return a `ToolResult`
  with an `Outcome` of `success` / `failure` / `unknown`.
- **`query_status`** — given an `intent_id`, report whether the side effect
  exists in the authoritative system. This is what makes crash recovery safe:
  recovery reconciles by querying, never by re-issuing.
- **`verify_postcondition`** — confirm observed state matches the declared
  expectation (catches *phantom success*, where a tool reports OK but did
  nothing).

## Subclassing `BaseAdapter` (recommended)

`BaseAdapter` provides the safety machinery so you only implement the happy path
and the post-condition:

```python
class MyAdapter(BaseAdapter):
    tools = ("do_thing", "undo_thing")     # tool names this adapter serves

    async def _do_execute(self, intent: ActionIntent) -> ToolResult:
        ...  # create the side effect, call self.world.record_effect(...)
        return ToolResult(intent_id=intent.intent_id, outcome=Outcome.SUCCESS,
                          data={"booking_id": ...})

    def _postcondition(self, intent, state) -> VerificationResult:
        ...  # check self.world reflects the effect
```

`BaseAdapter.execute` automatically weaves in, in order:

1. **Failure injection (before):** the `FailureInjector` can turn this call into
   an error, timeout, unknown outcome, malformed result, phantom success,
   rate-limit, or a crash *before* any side effect.
2. **Idempotent replay:** if `intent_id` already produced an effect, the
   recorded result is returned — no duplicate side effect.
3. **Happy path:** your `_do_execute`.
4. **Crash injection (after):** simulates a crash *after* the effect is durable
   but before the response is delivered (`CrashSignal(effect_applied=True)`),
   exercising the reconciliation path.

## Recording effects for idempotency + recovery

Call `self.world.record_effect(intent_id, kind, entity_id, data)` when you apply
a side effect. This:

- indexes the effect by `intent_id` so retries dedupe automatically, and
- lets the default `query_status` discover the effect during crash recovery.

If your adapter models an external system's own durable store, construct the
world with a `persist_path` so effects flush to disk and a fresh process can
query authoritative state after a crash.

## Registering an adapter

```python
world = MockWorldState()
registry = AdapterRegistry(world=world)
registry.register(MyAdapter(world, registry.injector))
adapter = registry.get("do_thing")
```

The registry shares one `world` and one `FailureInjector` across all adapters so
the whole run stays deterministic and reproducible.

## Reversibility & compensation metadata

The adapter *executes*; it does not decide reversibility. Tier classification
and the compensation for a tool come from the tool registry / workflow
(authority hierarchy: explicit → registry → adapter → LLM → none). A
compensation is dispatched as an ordinary `execute` call with
`ActionIntent.is_compensation = True` pointing at the inverse tool
(e.g. `cancel_booking`, `refund_payment`).

## A complete worked example

See [`examples/custom_tool.py`](../examples/custom_tool.py) for a runnable
`InventoryAdapter` that implements `reserve_inventory` / `release_inventory`
with post-conditions and a compensating call. Run it with:

```bash
python examples/custom_tool.py
```
