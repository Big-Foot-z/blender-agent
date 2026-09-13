# Localized UV Failure Repair Acceptance Gates

> 대응 문서: `LOCALIZED_UV_FAILURE_REPAIR_WORK_PLAN.ko.md`
> 판정: PASS / FAIL / BLOCKED / NOT_RUN
> 출시 기준: 필수 Gate 모두 PASS.

## G0 — 기준선

**필수.**

동일 source / Blender / code / profile / import normalization 조건을 고정한다.

baseline:

```text
island_count = 45
tiny_island_count = 17
one_two_face_island_count = 13
anisotropy_p95 = 1.354
worst_island_p95 = 1.848
anisotropy_max = 100.31
uv_degenerate_count = 0
local_flip_count = 0
overlap_area_total = 4.72e-4
packing_efficiency = 0.378
min_island_gap_px = 5.36
texel_density_cv = 5.2e-5
```

## G1 — Failure Localization

**필수.**

최종 UV에서 failure face를 triangle 단위로 식별한다.

필수 출력:

```text
catastrophic face ids
catastrophic cluster ids
island id
anisotropy value
overlap participation
```

통과 조건:

- anisotropy max triangle 누락 0
- overlap participating face 누락 0
- NaN/invalid 값을 정상 face로 처리하지 않음

## G2 — Overlap Type 분리

**필수.**

report에 반드시:

```text
self_overlap_area_total
cross_island_overlap_area_total
```

를 별도 기록.

검증 fixture:

- same-island overlap → self만 증가
- different-island overlap → cross만 증가
- boundary touch → 둘 다 0

## G3 — Correct Repair Priority

**필수.**

priority:

```text
degenerate/flip
> self-overlap
> catastrophic anisotropy
> ordinary distortion
```

상위 failure가 남아 있는데 하위 distortion refinement를 먼저 수행하면 FAIL.

## G4 — Target Island Isolation

**필수.**

한 repair round는 한 island만 대상으로 한다.

통과 조건:

- target island 명시
- target face set 명시
- non-target island UV 변경 0 또는 허용 rigid transform 이내
- non-target seam set 변경 0

## G5 — Self-overlap Repair

**필수.**

self-overlap fixture에서:

```text
self_overlap_area_after < before
```

최종 accepted라면:

```text
self_overlap_area_total <= 1e-8
```

기존 exact-overlap tolerance를 사용.

repair가 불가능하면 accepted 금지, `needs_user_review`.

## G6 — Cross-island Overlap Handling

**필수.**

cross-island overlap은 seam split보다 repack을 먼저 시도.

통과 조건:

- geometry seam 추가 전 repack attempt 존재
- pack으로 해결되면 seam delta = 0
- 최종 accepted 시 cross-island overlap <= 1e-8

## G7 — Catastrophic Anisotropy Gate

**필수.**

engineering default:

```text
catastrophic threshold = 10.0
```

최종 release에서는 frozen profile 사용.

통과 조건:

```text
anisotropy_max <= catastrophic cap
```

현재 baseline `100.31`은 HARD FAIL.

max triangle이 적은 면적이라고 무시하면 FAIL.

## G8 — Worst Island Quality

**필수.**

최종 accepted:

```text
worst island p95 <= frozen island p95 cap
```

baseline `1.848`이 cap 초과라면 반드시 repair 또는 review.

## G9 — Candidate Acceptance

**필수.**

candidate는 다음을 모두 만족해야 함:

```text
target failure 개선
correctness regression 없음
catastrophic regression 없음
fragmentation budget PASS
constraint PASS
```

단순 target metric 15% 개선만으로 accepted 금지.

## G10 — Rollback Completeness

**필수.**

reject/exception/cancel 후:

- seam set 동일
- UV coordinates 동일
- active UV layer 동일
- island transforms 동일
- temp state 제거

snapshot hash/epsilon 비교로 검증.

## G11 — Fragmentation Guard

**필수.**

repair 때문에 fragmentation이 다시 증가하면 안 됨.

accepted candidate 기준:

```text
new 1-face island = 0
new 2-face island = 0
```

단 unavoidable correctness repair는 reason과 함께 review 필요.

최종 real asset에서:

```text
tiny <= 17
1-2 face <= 13
```

최소한 baseline보다 악화 금지.

## G12 — Non-target Island Immutability

**필수.**

repair 전후 non-target island의:

```text
face membership
seam boundary
UV coordinates
```

비교.

전체 unwrap 호출을 사용하더라도 최종 결과에서 non-target island는 원복되어야 한다.

## G13 — Mandatory Seam Regression

**필수.**

기존 정상화된 mandatory 규칙 유지:

```text
mandatory_90_edges = actual fold set
mandatory_90_missing = 0
mandatory_90_uv_unsplit = 0
```

localized repair가 mandatory seam을 제거하면 FAIL.

## G14 — Degenerate / Flip

**필수.**

최종:

```text
uv_degenerate_count = 0
local_flip_count = 0
```

하나라도 남으면 accepted 금지.

## G15 — Final Overlap

**필수. HARD GATE.**

최종:

```text
self_overlap_area_total <= 1e-8
cross_island_overlap_area_total <= 1e-8
overlap_area_total <= 1e-8
```

현재 baseline `4.72e-4`는 FAIL.

## G16 — Packing / Gap

**필수.**

geometry repair가 완료된 후 final pack.

통과 조건:

- bounds PASS
- min island gap >= profile margin
- cross overlap 0
- texel density regression budget PASS

packing efficiency는 quality metric이지만 correctness보다 우선하지 않는다.

## G17 — Determinism

**필수.**

동일 입력 3회:

- target island 동일
- failure cluster 동일
- candidate ordering 동일
- final seam set 동일
- island count 동일
- float 차이 기존 deterministic tolerance 이내

## G18 — Debug Evidence

**필수.**

run마다:

```text
failure_localization.json
repair_history.json
repair_candidates.json
final_correctness.json
final_distortion.json
```

저장.

각 accepted/rejected candidate reason 추적 가능해야 함.

## G19 — Real Asset Acceptance

**출시 필수.**

현재 statue/lowpoly 결과를 동일 조건으로 재실행.

필수 조건:

```text
mandatory missing = 0
uv unsplit = 0
uv degenerate = 0
local flip = 0

self overlap <= 1e-8
cross overlap <= 1e-8

anisotropy max <= frozen cap
worst island p95 <= frozen cap

tiny island <= baseline
1-2 face island <= baseline

non-target island regression = 0
```

추가로 reviewer가 Blender 3D/UV view에서:

- repair 부위가 문제 영역과 일치
- 불필요한 새 seam 없음
- 큰 coherent island 유지

를 확인한다.

# Release Gate

다음이 모두 PASS여야 함:

```text
G0
G1
G2
G3
G4
G5
G6
G7
G8
G9
G10
G11
G12
G13
G14
G15
G16
G17
G18
G19
```

핵심 원칙:

> 모델 전체를 다시 자르지 않는다.
> 실패한 face cluster와 그 island만 복구한다.
> 최종 accepted 결과에서 correctness failure와 catastrophic local distortion은 0이어야 한다.
