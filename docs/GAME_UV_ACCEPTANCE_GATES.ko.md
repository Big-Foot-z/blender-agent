# 게임용 자동 UV Acceptance Gates

- 대응 문서: `GAME_UV_WORK_PLAN.ko.md`
- 대상 저장소: `Big-Foot-z/blender-agent`
- 기준 브랜치: `main`
- 기준 트리: `45a07056e89ea2630f73ef3a88522a6741f6250e`
- 판정 상태: `PASS / FAIL / BLOCKED / NOT_RUN`
- 출시 조건: **모든 HARD Gate PASS + holdout 실모델 reviewer 승인**
- 원칙: `BLOCKED`와 `NOT_RUN`은 `PASS`가 아니다.

---

## Gate 우선순위

```text
HARD correctness
-> HARD game-runtime compatibility
-> HARD distortion profile
-> HARD fragmentation/padding
-> optimization quality
-> reviewer acceptance
```

수치 threshold는 반드시 `profile_id + metric_version`으로 저장한다.
개별 asset 전용 예외 threshold는 허용하지 않는다.

---

## G0 — Reproducible Baseline

**HARD**

통과 조건:

- 입력 mesh SHA-256 저장.
- vertex/face/loop topology fingerprint 저장.
- Blender 버전, OS, 코드 SHA, UV profile, seed 저장.
- baseline/new 모두 동일 evaluator version 사용.
- UV 작업 전후 3D vertex position/topology/material assignment 변경 0.
- 동일 입력과 동일 설정 3회 실행 가능.

증거:

```text
run_manifest.json
mesh_identity.json
baseline_report.json
candidate_report.json
```

---

## G1 — Mandatory Seam Correctness

**HARD**

| 항목 | 통과 조건 |
|---|---|
| 89.9° | mandatory 아님 |
| 90.0° | mandatory |
| 90.1° | mandatory |
| mandatory 누락 | `mandatory_90_missing == 0` |
| 실제 UV 미분리 | `mandatory_90_uv_unsplit == 0` |
| non-manifold 진단 | 미진단 0 |
| boundary 처리 | 정의된 topology 정책과 일치 |

90° 비교 epsilon은 구현에서 명시하고 fixture로 고정한다.

`user_forbidden`과 mandatory seam이 충돌하면 mandatory가 이기며, 결과는 자동 `accepted`가 아니라 conflict report를 남긴다.

---

## G2 — Smooth Surface Preservation

**HARD 정책 / QUALITY 수치**

목표:

> `dihedral < 90°` 영역은 distortion/correctness가 요구하지 않는 한 추가 seam을 만들지 않는다.

통과 조건:

- distortion profile을 이미 통과한 island에 추가 seam 생성 0.
- packing efficiency만을 이유로 seam 추가 0.
- convexity만을 이유로 seam 추가 0.
- 후보 추가 seam은 reason에 `distortion_repair` 또는 명시적 user/material/shading 이유가 있어야 함.
- 동일 품질 후보가 여러 개면 island 수가 가장 적은 결과 선택.
- island 수도 동일하면 normalized seam length가 가장 짧은 결과 선택.

증거:

- candidate history
- rejected seam history
- before/after island count
- before/after normalized seam length

---

## G3 — UV Correctness

**HARD**

| 항목 | 통과 조건 |
|---|---|
| UV finite | NaN/Inf 0 |
| bounds | `[0,1] ± 1e-4` |
| degenerate UV triangle | 0 |
| unintentional local flip/fold | 0 |
| positive-area overlap | 0 |
| overlap area tolerance | tile area 기준 `<= 1e-8` |
| UV island connectivity | report와 실제 UV connectivity 일치 |

`raster_overlap_ratio`는 보조 지표로만 사용하며 exact triangle intersection 검사를 대체하지 않는다.

미러링/의도적 stacking은 현재 기본 game profile에서는 지원하지 않는다. 향후 지원 시 별도 profile로 분리한다.

---

## G4 — Distortion Metric Validity

**HARD**

분석적 fixture 통과 조건:

| fixture | 기대값 |
|---|---|
| planar isometry | anisotropy `1 ± 1e-6` |
| uniform UV scale | normalized distortion 변화 `<= 1e-6` |
| UV rotation | distortion 변화 `<= 1e-6` |
| U×4 / V×0.25 | anisotropy `16 ± 1e-6` |
| area-preserving shear | area distortion≈0, anisotropy>1 |
| local collapse | correctness FAIL |
| tiny bad patch | global 평균과 별도로 max/bad-area에 검출 |

필수 metric:

```text
metric_version
global_area_stretch_mean
global_area_stretch_p95
global_anisotropy_p95
global_anisotropy_max
worst_island_id
worst_island_area_stretch_p95
worst_island_anisotropy_p95
worst_island_anisotropy_max
bad_area_ratio
```

필수 metric이 누락되거나 NaN/Inf이면 `accepted` 금지.

---

## G5 — Game Distortion Quality Profile

**HARD — threshold는 calibration 후 freeze**

실제 수치는 소스 코드 상수가 아니라 versioned profile로 관리한다.

profile 필수 키:

```text
profile_id
metric_version
area_stretch_global_p95_max
area_stretch_island_p95_max
anisotropy_global_p95_max
anisotropy_island_p95_max
anisotropy_max_max
bad_area_threshold
bad_area_ratio_max
min_improvement_ratio
```

통과 조건:

- global metric 전부 profile 이내.
- worst island metric 전부 profile 이내.
- bad area ratio profile 이내.
- global 평균만 통과하고 worst island가 실패하면 FAIL.

초기 개발 중 기존 `0.35`, `0.5` 등의 값은 실험용으로 사용할 수 있지만 **출시 threshold로 자동 승격하지 않는다.**

---

## G6 — Refinement Candidate Acceptance / Revert

**HARD**

추가 seam 채택 조건:

1. correctness 회귀 없음.
2. mandatory/user constraint 위반 없음.
3. target island 실패 지표 개선.
4. 상대 개선율 `>= profile.min_improvement_ratio`, 또는 split 후 target island가 즉시 profile 통과.
5. fragmentation hard limit 위반 없음.
6. island cap 미초과.

거절 시 반드시 복원:

```text
seam set
UV coordinates
active UV layer
selection state if worker path depends on it
candidate-local metadata
```

예외/취소/timeout에서도 동일하다.

---

## G7 — Island Count / Fragmentation

**HARD + QUALITY**

게임용 UV에서는 island 수 자체보다 "불필요한 fragmentation"을 금지한다.

필수 지표:

```text
island_count
tiny_island_count
tiny_island_area_ratio
sliver_island_count
one_two_face_island_count
island_aspect_p95
normalized_seam_length
```

### HARD

- zero-area island 0.
- profile이 정의한 minimum UV area 미만의 비허용 island 0.
- profile이 정의한 sliver hard limit 초과 0.

### QUALITY

baseline이 이미 distortion profile을 통과했다면:

```text
new_island_count <= baseline_island_count
```

baseline이 distortion을 실패했다면 island 수 증가는 허용하지만 다음을 만족해야 한다.

```text
new result passes distortion profile
AND every accepted extra seam has recorded improvement
```

### Merge-back 필수 검증

최종 refinement 뒤 quality를 유지하며 제거 가능한 seam이 남아 있으면 FAIL.

즉:

```text
quality-preserving removable seam count == 0
```

또는 계산 비용 때문에 exhaustive가 불가능한 profile에서는 정해진 merge-back budget을 모두 소진했고 더 좋은 후보를 찾지 못했다는 기록이 있어야 한다.

---

## G8 — Texel Density

**HARD**

기본 profile은 uniform density다.

통과 조건:

- texel density finite.
- global variance가 profile cap 이내.
- island별 density outlier가 profile cap 이내.
- intentional density weighting이 있으면 asset metadata와 report에 명시.
- metadata 없는 임의 중요도 추정으로 density를 변경하지 않음.

pack 전/후 uniform global scale로 인해 normalized density ratio가 달라지면 FAIL.

---

## G9 — Pixel Padding / Mip Safety

**HARD**

profile 필수 키:

```text
texture_size_px
margin_px
border_margin_px
```

검사는 UV 거리값이 아니라 pixel-equivalent distance로 수행한다.

통과 조건:

- island-to-island 최소 간격 `>= margin_px`.
- island-to-tile-border 최소 간격 `>= border_margin_px`.
- rotation/repack 후 재검증.
- export/re-read 후 재검증.

개발 기본 예시는 `8 px @ 1024`를 사용할 수 있으나 출시값은 profile freeze 결과를 따른다.

---

## G10 — Game Shading / Tangent Compatibility

**HARD when enabled by profile**

profile:

```text
shading_uv_policy =
  preserve |
  split_normals_on_uv_seams |
  require_uv_seam_on_sharp_edges
```

### preserve

- UV generation이 기존 sharp/smooth 상태를 의도치 않게 변경하지 않음.

### split_normals_on_uv_seams

- seam boundary와 sharp edge set이 post-process 정책과 일치.
- 기존 sharp edge는 보존.

### require_uv_seam_on_sharp_edges

- sharp/hard normal boundary 중 UV가 연결된 edge 0.

normal-map용 fixture에서 tangent basis 계산/export가 실패하면 Gate FAIL.

---

## G11 — Packing

**HARD correctness / QUALITY efficiency**

HARD:

- overlap 0.
- bounds 정상.
- padding 충족.
- texel density 정책 보존.

QUALITY:

- packing efficiency는 profile floor 이상.
- floor 미달 시 먼저 orientation/packer 개선을 시도.
- packing efficiency를 높이기 위한 추가 seam 생성 금지.

packing efficiency가 낮아도 correctness/distortion이 우수한 결과를 불필요하게 찢어서 통과시키지 않는다.

---

## G12 — Determinism / Budget

**HARD**

동일 입력, 코드, Blender, profile, seed로 3회 실행:

```text
seam edge set identical
island connectivity identical
candidate accept/reject sequence identical
float metrics delta <= 1e-6
```

예외적으로 Blender packer가 플랫폼별 float 차이를 보이면 UV 좌표 byte equality 대신 topology/island/seam equality와 metric tolerance를 사용한다.

budget 필수:

```text
max_iterations
max_candidates_per_round
max_islands
time_budget_sec
```

budget 도달 시 품질 미달이면 `needs_user_review`; best-so-far를 `accepted`로 승격하지 않는다.

---

## G13 — Export Round Trip

**HARD**

최종 승인 파일은 export 전 메모리 상태만 보고 승인하지 않는다.

지원 포맷별 최소 1개 round-trip:

```text
FBX
GLTF/GLB (제품 지원 범위에 포함될 경우)
```

재읽기 후 검사:

- mesh identity/topology 정책 일치.
- UV set 존재.
- UV 좌표 finite.
- mandatory seam 실제 UV split 유지.
- overlap 0.
- bounds 정상.
- padding 충족.
- texel density profile 통과.
- shading/tangent policy 통과.

export/re-read 실패 시 이전 승인 asset을 유지하고 새 결과는 승인 금지.

---

## G14 — Status / Artifact Atomicity

**HARD**

| 상황 | 상태 |
|---|---|
| 모든 HARD gate 통과 | `accepted` 후보 |
| distortion 미달 | `needs_user_review` |
| budget 초과 + 품질 미달 | `needs_user_review` |
| metric 누락/NaN | `failed` 또는 `needs_user_review` |
| save/export 실패 | `failed` |
| worker crash/cancel | 이전 승인 유지 |
| user constraint 충돌 | `needs_user_review` |

모든 artifact는 임시 run directory에 먼저 작성·검증하고, 승인 포인터는 마지막에 원자적으로 교체한다.

프로세스 exit code 0만으로 성공 판정하지 않는다.

---

## G15 — Evidence / Reviewer Visibility

**HARD for release**

각 run에 다음 evidence가 있어야 한다.

```text
uv_layout.png or svg
checker_3d_front.png
checker_3d_side.png
distortion_heatmap.png
seam_overlay.png
quality_report.json
candidate_history.json
merge_back_history.json
export_reread_report.json
```

seam overlay는 최소한 다음 reason을 구분한다.

```text
mandatory_90
boundary/topology
user
shading
material
distortion_added
rejected_candidate
```

reviewer는 최소 다음을 확인할 수 있어야 한다.

- worst distortion island 위치
- 추가 seam 이유
- 추가 seam 전/후 개선량
- tiny/sliver island
- texel density 이상치
- padding failure 위치

---

## G16 — Calibration Dataset

**HARD for release**

최소 calibration set:

1. hard-surface asset 1+
2. organic asset 1+
3. elongated/cylindrical asset 1+
4. 실제 프로젝트 low-poly asset 1+

calibration에서 확정할 값:

- area stretch caps
- anisotropy caps
- bad-area cap
- min improvement ratio
- tiny/sliver 기준
- packing floor
- margin/border padding
- time/candidate/island budget

threshold 변경 시 기록:

```text
old profile
new profile
변경 이유
사용 asset 목록
reviewer verdict
```

한 모델만 통과시키기 위한 사후 완화 금지.

---

## G17 — Holdout Product Acceptance

**FINAL HARD GATE**

calibration에 사용하지 않은 holdout asset 최소 1개 이상을 포함한다.

모든 holdout에서:

```text
G1 mandatory seam PASS
G3 correctness PASS
G5 distortion profile PASS
G7 fragmentation PASS
G8 texel density PASS
G9 padding PASS
G10 shading policy PASS (해당 profile)
G13 export round-trip PASS
G14 status/artifact PASS
```

추가 비교 조건:

- baseline이 이미 품질 통과 상태였다면 island 수 증가 0.
- baseline이 품질 미달이었다면 증가한 seam 각각에 개선 근거 존재.
- 동등 품질에서는 normalized seam length가 더 짧거나 같음.
- reviewer가 checkerboard에서 눈에 띄는 왜곡/needle island/불필요 seam이 없다고 승인.
- 실패 asset을 결과표에서 제외하지 않음.

최종 상태:

```text
AUTOMATION_ACCEPTED = all hard gates pass
ARTIST_APPROVED = human reviewer explicit approval
SHIP_READY = AUTOMATION_ACCEPTED and ARTIST_APPROVED
```

---

## Release Checklist

```text
[ ] G0 baseline reproducible
[ ] G1 90° mandatory seam
[ ] G2 smooth preservation
[ ] G3 UV correctness
[ ] G4 metric validity
[ ] G5 distortion profile
[ ] G6 candidate accept/revert
[ ] G7 fragmentation + merge-back
[ ] G8 texel density
[ ] G9 padding
[ ] G10 shading/tangent compatibility
[ ] G11 packing
[ ] G12 determinism/budget
[ ] G13 export round-trip
[ ] G14 status/artifact atomicity
[ ] G15 review evidence
[ ] G16 calibration profile frozen
[ ] G17 holdout + reviewer approval
```
