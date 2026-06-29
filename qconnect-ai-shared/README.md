# qconnect-ai-shared

Shared building blocks used by both the **cloud** and **edge** projects of
QConnect-AI: Pydantic v2 schemas, enums, domain constants, custom exceptions and
small security helpers (JWT, AES-256-GCM).

Keeping these in one place guarantees that an evaluation request serialized at
the edge deserializes identically in the cloud — the wire contract lives here.

## Install (editable, for monorepo development)

```bash
pip install -e qconnect-ai-shared
```

## Modules

| Module | Contents |
|--------|----------|
| `shared.models` | Request/response Pydantic schemas (`QCDataInput`, `QCEvaluationResponse`, …) and domain enums |
| `shared.constants` | Westgard rule names, sigma category thresholds, LOINC mappings, defaults |
| `shared.exceptions` | Typed exception hierarchy (`QConnectError` and subclasses) |
| `shared.security` | JWT encode/decode, AES-256-GCM encrypt/decrypt, payload signing |
| `shared.utils` | Statistics helpers (mean/sd/cv, percentiles, z-score) with no heavy deps |

## Example

```python
from shared.models import QCDataInput, AnalyteType, QCLevelType

qc = QCDataInput(
    lab_id="lab-genova-001",
    analyzer_id="ABBOTT-ARCHITECT-001",
    analyte_code="HCV-AB",
    analyte_type=AnalyteType.SEROLOGY,
    qc_lot_id="QC-HCV-DIAMEX-202603-001",
    qc_level=QCLevelType.NORMAL,
    result_value=1.45,
    target_value=1.50,
    sd_value=0.08,
    operator_id="EMP00234",
)
print(qc.model_dump_json(indent=2))
```
