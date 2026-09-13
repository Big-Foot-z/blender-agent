# Localized UV Failure Repair 작업 계획서

> 대상 저장소: `Big-Foot-z/blender-agent`
> 현재 상태: GLB/GLTF topology normalization 적용 후 island 폭증 문제는 해소됨.
> 이번 목표: 모델 전체를 다시 자르지 않고, **실패한 face cluster / island만 국소적으로 복구**한다.
> 비목표: 90도 mandatory seam 정책, GLB import normalization, 전체 segmentation 엔진, packer 전체 교체.

## 1. 배경

최근 결과는 topology normalization 이후 다음 수준까지 개선되었다.

- `mandatory_90_edges = 323`
- `mandatory_90_fold_edges = 323`
- `mandatory_90_missing = 0`
- `mandatory_90_uv_unsplit = 0`
- `island_count = 45`
- `uv_degenerate_count = 0`
- `local_flip_count = 0`
- `anisotropy_p95 = 1.354`
- `worst_island_p95 = 1.848`
- `anisotropy_max = 100.31`
- `overlap_area_total = 4.72e-4`
- `tiny island count = 17`
- `1-2 face island count = 13`
- `packing efficiency = 0.378`

시각적으로는 대부분의 UV island가 coherent하게 유지되고 있으며, 실패는 일부 특정 3D surface cluster / UV island에 국소화되어 있다.

따라서 이번 단계는 전역 UV 재생성이 아니라 **localized failure repair**가 맞다.

## 2. 핵심 결론

파이프라인은 다음 순서로 수정한다.

```text
final-ish UV
  -> failure localization
  -> overlap 유형 분해
  -> catastrophic distortion cluster 검출
  -> failing island만 repair
  -> candidate re-evaluate
  -> accept / rollback
  -> final correctness + distortion audit
  -> final pack
```

문제 없는 island는 절대 건드리지 않는다.

## 3. Scope

이번 수정 범위는 아래 6개로 제한한다.

1. failing face localization
2. self-overlap / cross-island overlap 분리
3. catastrophic anisotropy cluster 검출
4. failing island only seam refinement
5. candidate rollback / state restoration
6. final correctness re-audit

이번 범위에서 제외:

- 90° seam rule 변경
- GLB merge/weld 정책 변경
- 전체 segmentation 재설계
- 전체 packer 교체
- global retopo
- texel density 정책 변경

## 4. Failure Localization

### 4.1 per-triangle failure extraction

최종 UV에 대해 triangle 단위로 다음을 추출한다.

```text
face_id
triangle_id
island_id
anisotropy
area_stretch
signed_uv_area
degenerate flag
overlap participation count
```

### 4.2 catastrophic distortion 기준

`anisotropy_max` 하나만 보고 끝내지 않고, face cluster를 만든다.

권장 초기 기준:

```text
catastrophic_anisotropy_threshold = 10.0
neighbor_expand_threshold = 4.0
```

동작:

```text
anisotropy >= 10
  -> seed face
  -> 인접 face 중 anisotropy >= 4를 cluster에 포함
```

threshold는 engineering default이며, 최종 출시는 calibration profile로 고정한다.

### 4.3 output

run report에 최소 다음을 기록한다.

```json
{
  "failure_localization": {
    "catastrophic_faces": [],
    "catastrophic_clusters": [],
    "overlap_face_pairs": [],
    "self_overlap_clusters": [],
    "cross_island_overlap_pairs": []
  }
}
```

Blender debug 선택용 face id artifact도 생성한다.

## 5. Overlap 분리

현재 `overlap_area_total` 하나로는 repair 방향을 정할 수 없다.

반드시 다음으로 분리한다.

```text
self_overlap_area_total
cross_island_overlap_area_total
```

정의:

- same UV island 내부 triangle pair overlap → self-overlap
- 서로 다른 UV island triangle pair overlap → cross-island overlap

### 5.1 self-overlap

self-overlap은 seam / unwrap topology 문제로 취급한다.

처리:

```text
overlap face cluster
  -> cluster boundary
  -> existing seam / island boundary까지 seam path 후보
  -> local unwrap
```

### 5.2 cross-island overlap

cross-island overlap은 우선 packing 문제로 처리한다.

처리 순서:

```text
repack only
  -> exact overlap audit
  -> 여전히 overlap이면 layout/constraint 문제로 escalation
```

cross-island overlap 때문에 geometry seam을 추가하지 않는다.

## 6. Repair Target Selection

priority:

```text
P0: degenerate / local flip
P1: self-overlap
P2: catastrophic anisotropy
P3: ordinary island distortion
```

현재 결과에서는 P1/P2가 주요 대상이다.

한 iteration에서 **한 failing island만** 처리한다.

target selection report:

```json
{
  "repair_target": {
    "priority": "self_overlap",
    "island_id": 12,
    "face_ids": [],
    "reason": "self_overlap_cluster"
  }
}
```

## 7. Candidate Seam Generation

후보 seam은 failing island 내부에서만 생성한다.

후보 유형:

### C1 — bad cluster boundary exit path

```text
bad cluster
  -> shortest low-cost path
  -> existing seam / island boundary
```

### C2 — overlap separator

self-overlap triangle cluster A/B를 분리할 수 있는 path.

### C3 — distortion relief path

catastrophic anisotropy cluster를 큰 island에서 분리하되 tiny island를 만들지 않는 path.

## 8. Candidate Cost

후보 seam path 점수:

```text
cost =
    seam_length_cost
  + visible_surface_cost
  + low_dihedral_cut_cost
  + tiny_island_penalty
  + one_two_face_island_penalty
  + sliver_island_penalty
```

benefit:

```text
benefit =
    self_overlap_reduction
  + catastrophic_anisotropy_reduction
  + p95_improvement
```

채택은 단순 `15% 개선`만으로 결정하지 않는다.

## 9. Candidate Apply / Measure / Rollback

모든 후보는 snapshot으로 감싼다.

```text
snapshot
  -> seam candidate apply
  -> unwrap failing island
  -> local + global audit
  -> accept/reject
  -> reject면 complete restore
```

restore 대상:

- seam set
- UV coordinates
- active UV layer
- island transform
- temporary pin/state
- pack state if modified

## 10. Accept 조건

### self-overlap repair 후보

```text
target self-overlap area 감소
AND
new degenerate = 0
AND
new local flip = 0
AND
cross-island overlap 증가 없음
AND
catastrophic anisotropy 악화 없음
AND
tiny/1-2 face island 예산 위반 없음
```

### catastrophic anisotropy repair 후보

```text
target cluster max anisotropy 감소 >= min_improvement
OR
catastrophic threshold 이하로 진입
```

동시에:

```text
correctness PASS
fragmentation regression budget PASS
```

## 11. Fragmentation Guard

현재 45 island 중 tiny 17, 1–2 face island 13이므로 repair가 fragmentation을 다시 악화시키면 안 된다.

초기 engineering guard:

```text
candidate island_delta <= +1
new 1-face island = 0
new 2-face island = 0 unless unavoidable correctness repair
```

불가피한 경우 reason을 명시하고 `needs_user_review`.

## 12. Local Unwrap

가능하면 전체 모델 unwrap을 피한다.

목표:

```text
failing island only
```

실제 Blender API 제약으로 전체 unwrap 호출이 필요하더라도:

- non-target island UV 좌표 snapshot
- unwrap 후 non-target island exact restore
- target island만 결과 유지

를 통해 논리적으로 local repair를 보장한다.

## 13. Final Re-audit

repair loop 종료 후 반드시 전체 최종 audit 수행.

필수:

```text
uv_degenerate_count = 0
local_flip_count = 0
self_overlap_area_total <= tolerance
cross_island_overlap_area_total <= tolerance
mandatory_90_missing = 0
mandatory_90_uv_unsplit = 0
```

distortion:

```text
global anisotropy p95 <= profile
worst island p95 <= profile
anisotropy max <= catastrophic/profile cap
```

## 14. Final Pack

모든 geometry-side repair가 끝난 뒤에만 pack한다.

pack 후 다시:

```text
cross-island overlap
min gap
bounds
texel density
```

재검사.

packing 문제 때문에 seam 추가 금지.

## 15. Debug Artifacts

각 run에 다음 저장:

```text
failure_localization.json
repair_history.json
repair_candidates.json
final_correctness.json
final_distortion.json
```

가능하면 Blender side debug:

```text
select catastrophic faces
select self-overlap faces
select repaired island
```

## 16. 테스트

### T1 — isolated catastrophic face

한 island 내 일부 triangle만 anisotropy 매우 높게 구성.

기대:

- 정확한 face cluster 선택
- 다른 island 불변
- max 감소

### T2 — self-overlap island

하나의 island 내부에서만 overlap.

기대:

- self-overlap로 분류
- seam repair
- cross-island packer 호출로 잘못 처리하지 않음

### T3 — cross-island overlap

두 island가 pack 과정에서 겹침.

기대:

- repack 우선
- geometry seam 추가 0

### T4 — candidate rollback

candidate가 overlap을 줄이지만 anisotropy를 악화.

기대:

- reject
- snapshot exact restore

### T5 — fragmentation guard

repair candidate가 1-face island 생성.

기대:

- reject 또는 needs_user_review
- 자동 accepted 금지

### T6 — unaffected island immutability

repair 전후 non-target island UV exact/epsilon 비교.

기대:

- UV change 0 또는 허용 transform만

## 17. 실제 모델 재실행

현재 모델을 동일 조건으로 재실행.

Before:

```text
islands = 45
tiny = 17
1-2 face = 13
anisotropy_p95 = 1.354
worst_island_p95 = 1.848
anisotropy_max = 100.31
overlap_area_total = 4.72e-4
```

After에서 확인:

```text
self-overlap <= tolerance
cross-island overlap <= tolerance
anisotropy_max <= frozen profile
worst island p95 <= frozen profile
non-target island regression 없음
tiny/1-2 island 증가 없음
```

## 18. 구현 우선순위

### Phase L1
- overlap self/cross 분해
- catastrophic face localization
- debug artifact

### Phase L2
- target island selection
- seam candidate generation

### Phase L3
- local unwrap
- snapshot rollback

### Phase L4
- final re-audit
- fragmentation guard

### Phase L5
- real asset rerun
- profile calibration
- release gate

## 19. 완료 정의

다음을 모두 만족하면 완료:

```text
1. 실패 face/island가 자동으로 정확히 localization 된다.
2. self-overlap과 cross-island overlap을 구분한다.
3. non-target island는 repair 중 변경되지 않는다.
4. catastrophic anisotropy cluster를 국소적으로 복구한다.
5. candidate 실패 시 완전 rollback 된다.
6. fragmentation이 다시 폭증하지 않는다.
7. final accepted 결과는 correctness + distortion gate를 모두 통과한다.
```
