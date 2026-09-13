# 게임용 자동 UV 개선 작업 계획서

> 대상 저장소: `Big-Foot-z/blender-agent`
> 기준 브랜치: `main`
> 기준 트리: `45a07056e89ea2630f73ef3a88522a6741f6250e`
> 목적: 현재의 규칙 기반 seam + distortion refinement 구조를 유지하면서, 실제 게임 에셋에 사용할 수 있는 UV 품질 기준으로 고도화한다.
> 핵심 방향: **새 UV 엔진을 만들지 않고 `chart_uv_agent`를 geometry baseline으로 유지하며, `artist_uv_agent`의 seam policy/refinement/report 계층과 correctness/gate 계층을 강화한다.**

---

## 1. 현재 저장소에 대한 판단

현재 저장소는 이미 다음 구조를 갖고 있다.

```text
uv_agent/
  geometry/
    evaluation.py
    distortion_v2.py
    uv_correctness.py
    uv_gate.py
    packing.py

chart_uv_agent/
  segmentation.py
  pipeline.py
  refinement_loop.py
  quality_profile.py
  gate.py
  reporting.py

artist_uv_agent/
  seam_policy.py
  seam_refinement.py
  seam_report.py
  region_policy.py
  user_seams.py
  seams.py

worker/
  generate_uv_from_seams.py
  app_uv_generate_contract.py
  export_production_asset.py

tests/
  test_seam_core.py
  test_distortion_v2.py
  test_uv_correctness.py
  test_refinement_loop.py
  test_quality_profile.py
  e2e/test_uv_auto_gates.py
  e2e/test_uv_auto_export_perf.py
```

현재 seam core에도 이미 다음 원칙이 구현되어 있다.

1. `dihedral >= 90°`는 mandatory seam.
2. 낮은 각도의 smooth edge는 seam을 억제.
3. unwrap 후 worst island distortion을 측정하고 필요할 때만 추가 seam 검토.
4. 추가 seam은 distortion 개선량이 충분할 때만 유지.
5. island cap은 안전장치로 사용.
6. exact overlap / bounds / orientation / export re-read 검증 경로가 존재.

따라서 다음 단계의 핵심은 "규칙 3개를 다시 구현"하는 것이 아니다.

**게임용 사용성을 위해 seam 비용, sliver/tiny island, texel density, padding, normal/tangent 호환, export 재검증을 하나의 품질 프로파일 아래 묶고, island 수 최소화가 distortion과 충돌할 때의 의사결정 순서를 명확히 만드는 것**이 핵심이다.

---

## 2. 제품 목표

입력:

```text
UV가 없거나 재생성이 필요한 low-poly game mesh
```

출력:

```text
- 게임용 텍스처 UV
- UV seam set
- packed 0-1 UV
- checker / distortion heatmap
- machine-readable quality report
- export 후 재검증 결과
- 필요 시 needs_user_review 상태
```

성공한 UV는 다음 성질을 가져야 한다.

- 90° 이상 강한 fold는 반드시 분리된다.
- 90° 미만 smooth surface는 품질 기준을 통과하는 한 가능한 한 연결 상태를 유지한다.
- island 수와 seam 총 길이는 최소화한다.
- distortion이 기준을 초과하는 영역만 국소적으로 추가 절개한다.
- 작은 조각, needle/sliver island, 과도한 fragmentation을 억제한다.
- texture padding과 texel density가 게임 런타임/mipmap에 적합하다.
- normal map/tangent-space 파이프라인과 충돌하지 않는다.
- FBX/GLTF 등 최종 export 후에도 UV와 seam 의미가 보존된다.

---

## 3. 의사결정 원칙

가중합 하나로 모든 목표를 섞지 않는다.

다음 **lexicographic priority**를 사용한다.

```text
P0. correctness / topology / mandatory seam
P1. distortion quality profile 통과
P2. island 수 최소화
P3. seam 총 길이 최소화
P4. visible seam cost 최소화
P5. packing efficiency 최대화
```

즉:

- correctness를 깨면서 island 수를 줄이지 않는다.
- distortion 기준을 넘기면서 island 수를 줄이지 않는다.
- distortion 기준을 이미 통과한 상태라면 추가 seam을 만들지 않는다.
- 동일 품질을 만드는 후보가 여러 개면 island 수가 적은 것을 선택한다.
- island 수도 같으면 seam 총 길이가 짧은 것을 선택한다.
- 그 다음 hidden/back/concave 쪽 seam을 선호한다.

이 순서가 이번 작업의 핵심 계약이다.

---

## 4. 게임용 seam 정책 v2

### 4.1 Mandatory seam

다음은 무조건 분리한다.

```text
- dihedral >= 90°
- non-manifold 경계
- UV chart를 정의할 수 없는 topology boundary
```

추가 정책은 quality profile에서 선택 가능하게 한다.

```text
normal_map_profile:
  hard/sharp normal split edge -> UV seam required
```

이 옵션은 baked normal map/tangent-space 게임 에셋용 프로파일에서 ON을 권장한다.

### 4.2 Smooth preservation

`dihedral < 90°` edge는 기본적으로 seam이 아니다.

현재 `smooth_preserve_angle`은 candidate scoring bias로 유지하되, 실제 동작 규칙은 다음으로 명확히 한다.

```text
if edge < 90°:
  distortion/correctness 문제가 없으면 절대 추가 절개하지 않음
  distortion 문제가 있을 때만 candidate pool에 진입
```

즉 45° 같은 보조 threshold는 "자르는 기준"이 아니라 후보 ranking에만 사용한다.

### 4.3 Candidate seam cost

추가 seam 후보 비용:

```text
cost =
  normalized_seam_length
  + visible_surface_penalty
  + smooth_surface_penalty
  + small_island_creation_penalty
  + sliver_creation_penalty
  + protected_region_penalty
  - hidden_back_bonus
  - concave_crease_bonus
  - material_boundary_bonus
  - user_preferred_bonus
```

금지:

- packing 효율만 올리기 위한 seam 추가
- convexity만 개선하기 위한 seam 추가
- 이미 distortion profile을 통과한 island에 seam 추가

---

## 5. Distortion 평가 v2 통합

단일 `stretch_score`만으로 판단하지 않는다.

필수 측정값:

```text
global:
  area_stretch_mean
  area_stretch_p95
  anisotropy_p95
  anisotropy_max
  angle_distortion_p95

per island:
  area_stretch_mean
  area_stretch_p95
  anisotropy_p95
  anisotropy_max
  bad_area_ratio

per face / heatmap:
  stretch
  anisotropy
```

판정 원칙:

- global 평균이 좋아도 worst island가 나쁘면 실패.
- 작은 면적의 극단적 distortion도 `max`와 `bad_area_ratio`로 노출.
- degenerate/collapsed UV는 좋은 점수로 취급하지 않고 correctness fail.
- uniform scale/rotation/translation은 distortion metric을 바꾸지 않아야 한다.

---

## 6. Refinement loop 개선

현재 one-worst-island-per-round 구조를 유지한다.

### 6.1 루프

```text
1. mandatory seam으로 초기 unwrap
2. quality 측정
3. profile 통과 -> 종료
4. worst failing island 선택
5. 후보 seam path N개 생성
6. 후보별 임시 unwrap
7. correctness 검사
8. distortion 개선량 검사
9. island/sliver/seam cost 검사
10. lexicographic ranking
11. best candidate 1개만 채택
12. 반복
```

### 6.2 후보 채택 조건

추가 seam은 다음을 모두 만족해야 한다.

```text
A. correctness 회귀 없음
B. target island의 실패 지표가 유의미하게 개선
C. 최소 개선율 >= profile.min_improvement_ratio
   OR 해당 split으로 target island가 즉시 profile 통과
D. 새 sliver/tiny island가 hard limit을 넘지 않음
E. island cap 미초과
```

현재 기본 `min_improvement_ratio=0.15`는 초기값으로 유지하되 최종값은 fixture calibration에서 고정한다.

### 6.3 Merge-back pass

현재 구조에 추가할 중요 단계다.

refinement가 끝난 뒤 인접 island pair를 대상으로 seam 제거를 시험한다.

```text
for removable seam:
  merge -> unwrap -> evaluate
  if correctness + distortion profile 유지:
      merge 채택
```

목표:

- refinement가 필요 이상으로 만든 seam 제거
- "distortion을 맞춘 뒤 island 수를 다시 최소화" 구현

이 단계는 게임용 목표에서 중요하다.

---

## 7. Island 품질 규칙

단순 island count 외에 다음을 추가한다.

### Hard

- UV triangle degenerate 0
- unintentional overlap 0
- UV bounds 정상
- profile padding 충족
- zero-area island 0

### Fragmentation quality

- `tiny_island_area_ratio`
- `tiny_island_count`
- `sliver_island_count`
- `island_bbox_aspect_ratio`
- `uv_area / bbox_area`
- 1~2 face island 비율

예외:

```text
명시적으로 detail/cap으로 분류된 island
필수 90° seam 때문에 구조적으로 생기는 작은 island
```

예외도 report에는 남긴다.

---

## 8. Texel density와 packing

### 8.1 Texel density

기본 게임 프로파일:

```text
uniform texel density
```

의도적 density weighting은 별도 profile/metadata가 있을 때만 허용한다.

검사:

- global density variance
- island별 relative density
- 이상치 island 목록

### 8.2 Padding

padding은 UV 단위가 아니라 texture pixel 기준으로 정의한다.

```text
margin_px
texture_size_px
```

검증기는 실제 island 간 최소 거리와 tile border 거리를 pixel로 환산하여 검사한다.

초기 개발 기본값은 저장소의 기존 실행 예와 맞춰 `8 px @ 1024`를 사용 가능하지만, 출시 기준은 asset/engine 요구에 따라 versioned profile로 고정한다.

### 8.3 Packing

packing은 seam 생성 이유가 아니다.

순서:

```text
seam/refinement 완료
-> density normalize
-> orientation optimization
-> pack
-> padding/correctness 재검증
```

packing efficiency가 낮아도 UV 품질이 좋다면 먼저 rotation/layout/packer 개선을 시도하고, island 추가 분할은 금지한다.

---

## 9. Normal / tangent-space 게임 호환

게임용 export profile에 다음 설정을 둔다.

```text
shading_uv_policy:
  preserve
  split_normals_on_uv_seams
  require_uv_seam_on_sharp_edges
```

기본 제안:

- 일반 color/albedo UV: `preserve`
- tangent-space normal map용 static mesh: `require_uv_seam_on_sharp_edges` 검토
- 기존 저장소의 `split_smoothing_by_uv_islands`는 후처리 옵션으로 유지

Acceptance에서는 "UV가 좋아 보이는가"뿐 아니라 export 후 normal/tangent 계약이 깨지지 않는지 검사한다.

---

## 10. 구현 작업 단위

### W1 — Game quality profile

대상:

```text
chart_uv_agent/quality_profile.py
```

추가:

- `profile_id`
- `metric_version`
- global/island distortion thresholds
- bad-area threshold
- padding px / texture size
- tiny/sliver island limits
- min improvement ratio
- iteration/candidate/island/time budgets
- normal/tangent policy

완료 조건:

- JSON 직렬화 가능
- report에 active profile 전체 저장
- threshold 누락 시 accepted 금지

### W2 — Seam cost + candidate ranking

대상:

```text
artist_uv_agent/seam_policy.py
chart_uv_agent/candidates.py
chart_uv_agent/constraints.py
```

추가:

- normalized seam length
- tiny/sliver creation prediction
- visibility/hidden-side cost
- material/sharp edge signal
- reason code 표준화

완료 조건:

- 모든 후보가 score가 아닌 구조화된 reason/cost breakdown을 가짐
- deterministic tie-break 존재

### W3 — Distortion profile evaluator

대상:

```text
uv_agent/geometry/distortion_v2.py
uv_agent/geometry/evaluation.py
artist_uv_agent/seam_refinement.py
```

추가:

- global/worst island 동시 판정
- anisotropy/area/angle 결합 gate
- bad area ratio
- heatmap artifact

### W4 — Merge-back optimizer

신규 제안:

```text
chart_uv_agent/merge_back.py
```

역할:

- removable seam 탐색
- seam 하나씩 제거 trial
- quality 유지 시 island merge
- island count / seam length 감소량 기록

### W5 — Fragmentation metrics

대상:

```text
uv_agent/geometry/evaluation.py
chart_uv_agent/gate.py
artist_uv_agent/seam_report.py
```

추가:

- tiny/sliver metrics
- island aspect distribution
- 1~2 face island 통계

### W6 — Game packing / padding verification

대상:

```text
uv_agent/geometry/packing.py
uv_agent/geometry/uv_correctness.py
```

추가:

- exact pixel padding audit
- border padding audit
- post-pack metric invariance test

### W7 — Export round-trip validation

대상:

```text
uv_agent/blender/export.py
uv_agent/blender/export_validation.py
worker/export_production_asset.py
```

필수:

- export 후 재읽기
- UV set 존재/active 여부
- UV coordinates/fingerprint 비교
- overlap/bounds/padding 재측정
- seam/normal split 정책 재검증

### W8 — Report / UI evidence

대상:

```text
artist_uv_agent/seam_report.py
chart_uv_agent/reporting.py
app/electron/renderer/src/uv-generate/
```

표시:

- mandatory seam
- added distortion seam
- user seam
- protected edge
- rejected candidate
- distortion heatmap
- per-island failure
- before/after improvement
- merge-back history

---

## 11. 테스트 계획

### Unit fixtures

필수 fixture:

1. 89.9° / 90.0° / 90.1° edge
2. planar strip
3. cylinder + caps
4. bevel cube
5. smooth curved band
6. tiny severe-distortion patch
7. sliver-producing split candidate
8. overlap / local fold fixture
9. sharp-normal boundary fixture
10. merge-back 가능한 over-segmented fixture

### E2E fixture

최소 세 종류:

```text
hard-surface prop
organic character/creature
elongated/cylindrical prop
```

가능하면 현재 사용 중인 실제 `lowpoly.fbx`를 calibration fixture로 추가하되, 해당 모델 하나만 맞추는 threshold 튜닝은 금지한다.

---

## 12. 단계별 실행 순서

### Phase 1 — 기준선 고정

- current main에서 동일 asset 실행
- code SHA / Blender version / seed / profile 저장
- current UV, seam set, metrics, export 결과 보존

### Phase 2 — 측정 먼저 개선

- distortion_v2 검증
- tiny/sliver/padding metrics 추가
- acceptance report부터 완성

### Phase 3 — seam cost 개선

- candidate cost breakdown
- deterministic ranking
- visible/smooth seam 억제

### Phase 4 — refinement + merge-back

- worst island split 유지
- 후보 trial/revert 강화
- quality 통과 후 merge-back

### Phase 5 — game export 검증

- normal/tangent profile
- FBX/GLTF round-trip
- post-export gate

### Phase 6 — calibration

- calibration fixture set으로 threshold 탐색
- reviewer checker 검토
- profile version freeze

### Phase 7 — holdout acceptance

- calibration에 쓰지 않은 모델 실행
- 모든 hard gate 통과
- 기존 baseline 대비 fragmentation/seam length 개선 확인

---

## 13. 비목표

이번 작업에서 하지 않는다.

- LLM이 raw UV coordinate 생성
- semantic body-part recognition을 필수 조건으로 도입
- packing 효율을 위해 무조건 island 증가
- Smart UV Project를 최종 fallback으로 사용해 accepted 처리
- 개별 모델을 통과시키기 위한 threshold 예외 하드코딩
- 기존 low-poly topology 수정

---

## 14. Definition of Done

작업은 다음이 모두 충족될 때 완료로 본다.

```text
[ ] mandatory 90° seam correctness 통과
[ ] smooth surface 불필요 seam 억제 검증
[ ] global + worst-island distortion profile 통과
[ ] refinement split은 유의미한 개선이 있을 때만 유지
[ ] merge-back으로 품질 유지 가능한 불필요 seam 제거
[ ] tiny/sliver fragmentation gate 통과
[ ] exact overlap/bounds/padding gate 통과
[ ] texel density gate 통과
[ ] game shading profile 계약 통과
[ ] export/re-read 후 동일 gate 재통과
[ ] deterministic 3-run 결과 통과
[ ] calibration + holdout 실모델 검증 완료
[ ] reviewer가 checker/seam/island 구성 승인
```

세부 수치 판정은 `GAME_UV_ACCEPTANCE_GATES.ko.md`를 단일 기준으로 사용한다.
