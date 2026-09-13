# 게임용 자동 UV Acceptance Gate 실행 결과

- 대응 문서: [Acceptance Gate](GAME_UV_ACCEPTANCE_GATES.ko.md), [작업 계획서](GAME_UV_WORK_PLAN.ko.md)
- 계획서 기준 트리: `45a0705`. 판정 기준 코드: `2b3e4fc` (main). 모든 evidence 는 이 커밋의 코드로 실행했다(본 문서 커밋은 문서만 추가한다).
- 실행 환경: Windows 10 Home 10.0.19045 x64, AMD64 12 core, 49,078 MB RAM, Python 3.14.4 (uv), Node/npm 11, Blender 5.1.2 (build hash ec6e62d40fa9, 2026-05-19)
- 증거 경로: `docs/evidence/game_uv_2b3e4fc/` (e2e evidence JSON 11개 + `real_humanstatue/` 실모델 요약 6개), 본 문서의 표.
- 판정은 메인이 최종 코드 기준 raw 출력(요약 JSON, 리포트 JSON, pytest/npm 출력)을 직접 대조해 내렸다. 기존 원장 `UV_AUTOMATION_GATE_RESULTS.ko.md` 는 수정하지 않았다.
- 상태값: PASS / FAIL / BLOCKED(입력·리뷰어·환경 부재) / NOT_RUN. BLOCKED 와 NOT_RUN 은 PASS 가 아니다. profile 은 `engineering_v0` (`calibrated = false`) 이며 수치 threshold 는 미보정 공학 초기값이다.

## 요약 표

| Gate | 상태 | 코드/설정 | 증거 경로 | 실패 이유/다음 조치 |
|---|---|---|---|---|
| G0 | PASS | 2b3e4fc, engineering_v0 | `run_manifest.json`, `mesh_identity.json`, `test_fixture_models.py`, `uv_auto_gates_test_determinism_three_runs_*.json` | — |
| G1 | PASS | 2b3e4fc | `uv_auto_gates_test_angle_boundary_rule.json`, `uv_auto_gates_test_auto_generate_all_fixtures.json`, `tests/test_mesh_graph_v2.py`, `tests/test_segmentation_constraints.py` | — |
| G2 | PASS | 2b3e4fc | `candidate_history.json`, `seam_overlay.json`(`rejected_candidates`), `tests/test_seam_candidates.py`, `tests/test_refinement_loop.py` | — |
| G3 | PASS | 2b3e4fc | `correctness.json`, `final_reread_audit.json`, `tests/test_uv_correctness.py` | — |
| G4 | PASS | metric_version 2 | `tests/test_distortion_v2.py`, `distortion_v2.json` `summary` 블록 | — |
| G5 | PASS (engineering_v0) / threshold freeze 는 G16 BLOCKED | engineering_v0 (calibrated=false) | `quality_profile.json`, `quality_report.json`, `tests/test_quality_profile.py` | 출시 threshold 는 calibration 후 확정 |
| G6 | PASS | 2b3e4fc | `tests/test_refinement_loop.py`, `tests/test_quality_profile.py`, e2e bevel_cube (2 round / 6 candidate) | — |
| G7 | PASS (synthetic 11 fixture) / 실모델 FAIL (G17 참고) | engineering_v0 | `merge_back_history.json`, `quality_report.json`, `tests/test_fragmentation.py`, `tests/test_merge_back.py` | humanstatue 는 fragmentation hard 실패로 needs_user_review |
| G8 | PASS | engineering_v0 | `uv_generate_summary.json` `texel_density`, `tests/test_texel_density.py` | — |
| G9 | PASS | 4 px / border 4 px @ 1024 | `correctness.json` `island_gap`/`border_gap`, `tests/test_uv_correctness.py`, `tests/test_refinement_loop.py`(border inset) | — |
| G10 | PASS (`preserve`, e2e+실모델) / `split_normals_on_uv_seams`·`require_uv_seam_on_sharp_edges` 는 단위 fixture 검증 | engineering_v0 | `shading_policy.json`, `export_reread_report.json`(tangent_ok), `tests/test_shading_policy.py`, `tests/test_pipeline_auto_integration.py` | 실 normal-map 에셋 e2e 는 G16 데이터 확보 후 |
| G11 | PASS (HARD) / QUALITY advisory 보고만 | engineering_v0 | `quality_report.json` `sections.packing`, `history` `gap_repack` | packing floor 는 미보정 |
| G12 | PASS | seed 0 | `uv_auto_gates_test_determinism_three_runs_bevel_cube.json`, `..._suzanne.json` | — |
| G13 | PASS (FBX, GLB) | 2b3e4fc | `uv_export_reread.json`, `uv_export_refused.json`, `export_reread_report.json` | OBJ/GLTF separate 는 제품 지원 범위 밖으로 미실행 |
| G14 | PASS | 2b3e4fc | `status.json`, `tests/test_uv_generate_mode_contract.py`, `app/test/integration.test.ts` | — |
| G15 | PASS (artifact·데이터·IPC·UI 구현) / GUI walkthrough BLOCKED | 2b3e4fc | run 디렉터리 artifact 목록, `app/test/*.ts` | 렌더러 수동 walkthrough 는 자동화 하네스 없음 |
| G16 | BLOCKED | engineering_v0 (calibrated=false) | `real_humanstatue/` | calibration set(hard-surface/organic/elongated/실프로젝트 각 1+)과 리뷰어 승인/거절 UV 쌍 없음 |
| G17 | BLOCKED (실행은 완료, 결과 needs_user_review) | 2b3e4fc | `real_humanstatue/uv_generate_summary.json` | holdout 실모델 1건 실행 결과는 FAIL(아래 표). 리뷰어 승인 없음 |

명령 세트(최종 코드, 각 1회): `uv run python -m pytest tests --ignore=tests/e2e` exit 0 (skip 3건: `tests/test_obj_loader.py` 2건 sample/uv_no.obj 부재, `tests/test_uv_review_artifacts.py` 1건 구식 Blender 탐지 — 기존 원장과 동일, 영향 Gate 없음); `npm run typecheck` exit 0; `npm run test:integration` exit 0, tests 42 / pass 42 / fail 0; `npm run build` exit 0. Blender e2e: `tests/e2e/test_fixture_models.py test_uv_auto_generate.py test_uv_auto_gates.py test_uv_auto_export_perf.py` 20건 중 19건 통과, 1건(`test_korean_space_path`) 은 Blender 프로세스가 기동 단계에서 정지해 600 초 subprocess timeout — worker 자체는 6 초 만에 `accepted` 로 완료하고 artifact 를 모두 기록했다(run 디렉터리 확인). 같은 테스트를 단독으로 1회 재실행해 통과했고 그 evidence 가 `uv_auto_gates_test_korean_space_path.json` 이다(`status: accepted`, exit 0). 코드 결함이 아닌 환경 flake 로 기록한다.

## 최종 실행 결과 표 (auto_generate, 기본 옵션, Blender 5.1.2, 코드 2b3e4fc)

| fixture | status | gate failures | islands | merge-back (reason / trials / remaining) | min gap px | termination (reason / iter / cand / s) | aniso p95 / area p95 / bad area | quality advisory |
|---|---|---|---|---|---|---|---|---|
| plane_grid | accepted | — | 1 | no_removable_seam / 0 / 0 | (단일 island) | quality_passed / 0 / 0 / 0.5 | 1.001 / 0.001 / 0 | — |
| cube | accepted | — | 6 | no_removable_seam / 0 / 0 | 4.03 | quality_passed / 0 / 0 / 5.3 | 1.000 / 0.000 / 0 | — |
| bevel_cube | accepted | — | 4 | no_removable_seam / 4 / 0 | 4.37 | quality_passed / 2 / 6 / 45.7 | 1.055 / 0.031 / 0 | — |
| cylinder_capped | accepted | — | 4 | no_removable_seam / 1 / 0 | 4.97 | quality_passed / 0 / 0 / 16.5 | 1.004 / 0.002 / 0 | — |
| uv_sphere | accepted | — | 2 | no_removable_seam / 1 / 0 | 4.02 | quality_passed / 0 / 0 / 6.5 | 1.505 / 0.295 / 0.018 | — |
| torus | accepted | — | 3 | no_removable_seam / 3 / 0 | 6.55 | quality_passed / 0 / 0 / 15.2 | 1.296 / 0.192 / 0.005 | — |
| suzanne | needs_user_review | quality_profile_failed, correctness_failed(overlap, orientation, island_gap), reread_audit_failed | 12 | skipped_quality_failed / 0 / 4 | 2.14 | no_improving_candidate / 1 / 3 / 22.2 | 1.977 / 0.495 / 0.115 | — |
| concave_plate | accepted | — | 10 | no_removable_seam / 0 / 0 | 4.16 | quality_passed / 0 / 0 / 8.3 | 1.321 / 0.250 / 0 | island_aspect_p95 10.0 > 6.0 |
| angle_boundary | accepted | — | 5 | no_removable_seam / 0 / 0 | 5.44 | quality_passed / 0 / 0 / 12.8 | 1.000 / 0.000 / 0 | — |
| degenerate_input | needs_user_review | input_defects | 10 | no_removable_seam / 0 / 0 | 5.09 | quality_passed / 0 / 0 / 28.9 | 1.000 / 0.000 / 0 | island_aspect_p95 63.5 |
| protected_path | accepted | — | 1 | no_removable_seam / 0 / 0 | (단일 island) | quality_passed / 0 / 0 / 0.8 | 1.011 / 0.010 / 0 | — |

모든 fixture 에서 mandatory_90_missing = 0, mandatory_90_uv_unsplit = 0, mesh identity 불변, fragmentation hard 통과, texel density cv ≤ 2.2e-3, border gap = 4.0 px, shading(`preserve`) 통과, 모든 accepted 런의 `quality_report_passed = true`. island 수는 기존 원장(082f70e)과 전 fixture 동일(증가 0). 실패 사례를 제외하지 않는다: suzanne 은 미보정 profile 과 overlap 으로 needs_user_review, degenerate_input 은 입력 결함 진단으로 accepted 금지.

## G0 — 재현 가능한 기준선

- `run_manifest.json`: commit SHA, platform, Blender 버전/빌드 해시, Python, 모델 SHA-256, mesh fingerprint, mode, seed, 옵션, 전체 profile. `mesh_identity.json`: 전후 fingerprint 비교 `unchanged = true` (11/11 fixture, 실모델 포함).
- fixture 빌드 결정성(`test_fixture_build_is_deterministic`) 통과. 동일 입력 3회 실행(G12) 동일.
- baseline 비교는 기존 원장 표(082f70e, 동일 evaluator v2)와 island 수/status 를 대조했다.

## G1 — Mandatory seam correctness

- 각도 경계(`test_angle_boundary_rule`): 89.9° `in_seam_set=false`, 90.0°/90.1° `mandatory_90`; epsilon 은 `|angle−90| ≤ 1e-5` snap (MeshGraph 생성 시점).
- 10개 정상 fixture 와 실모델에서 missing 0 / uv_unsplit 0, 저장 파일 재읽기 후에도 0 (`final_reread_audit`).
- non-manifold/면적 0/입력 퇴화: degenerate_input 이 `input_defects` 로 진단되어 accepted 금지.
- `user_forbidden` vs mandatory 충돌: `SeamConstraints` 가 `mandatory_wins` conflict 로 기록하고 contract 가 `pending_conflict` → needs_user_review (`tests/test_segmentation_constraints.py`, `tests/test_uv_generate_mode_contract.py`).

## G2 — Smooth surface preservation

- 후보 seam 은 `select_target` 이 profile 실패 island(또는 global 실패의 최악 island, correctness repair)만 고르므로 profile 을 통과한 상태에서 추가 seam 0. 모든 fixture 의 `candidate_history` 는 실패 island 에서만 생성됐다(bevel_cube 2 round 6 후보, suzanne 1 round 3 후보, 나머지 0).
- 후보 `cut_reason` ∈ {distortion_repair, correctness_repair}, `cost` breakdown(normalized_seam_length, visible/smooth/small-island/sliver/protected penalty, hidden/crease/material/preferred bonus) 기록. ranking 은 lexicographic (island 수 → normalized seam length → visible cost → total cost).
- packing/convexity 단독 사유 절개 0: gap 실패는 `repack_for_gap` 재배치만(history `gap_repack`).
- 거절 후보는 `seam_overlay.json` `rejected_candidates` 와 `seam_overlay.png`(magenta dashed) 에 남는다.

## G3 — UV correctness

- exact triangle overlap(≤1e-8), orientation(island 다수결, 국소 flip 0), degenerate 0, bounds [0,1]±1e-4, island gap, border gap, island connectivity(seam flood vs UV connectivity, 불일치 시 `island_connectivity_mismatch` 로 accepted 금지). uv_sphere merge-back 시험은 이 검사로 거절됐다(정당).

## G4 — Distortion metric validity

- 분석 fixture(`tests/test_distortion_v2.py`): 등각 1±1e-6, 균일 scale/rotation/translation 변화 ≤1e-6, ×4/×0.25 = 16±1e-6, area-preserving shear, 국소 collapse → correctness fail, tiny bad patch → max/bad-area 노출, NaN → invalid.
- `summary` 블록이 G4 필수 metric 10개(+ `bad_area_threshold`, `summary_valid`)를 그대로 싣는다.

## G5 — Distortion quality profile

- profile 필수 키 전부 존재(`REQUIRED_PROFILE_KEYS`, 누락/미지 키/미지 shading policy 는 ValueError). global + worst island + bad area 동시 판정, global 통과·island 실패는 FAIL.
- engineering_v0 값: anisotropy p95 1.6 global / 1.8 island, max 3.0, area stretch mean 0.50/0.60, p95 0.9/1.1, bad area basis 1.6 / ratio 0.05 global / 0.10 island. 미보정이며 출시 threshold 로 승격하지 않는다(G16).

## G6 — Candidate acceptance / revert

- `accept_candidate` 순서: correctness 회귀 → 제약 → fragmentation hard 한도(G6-D) → 회귀 예산 → 전체 통과 → 개선율 ≥ 0.15. 거절/예외/악화 후보 후 seam 집합·UV 배열 byte 동일(`test_rejected_candidate_restores_seams_and_uvs`).
- selection state: worker 경로는 unwrap 시 `select_all` 을 다시 수행하므로 selection 에 의존하지 않는다(기록만).

## G7 — Island count / fragmentation / merge-back

- 지표: island_count, tiny_island_count, tiny_island_area_ratio, sliver_island_count, one_two_face_island_count, island_aspect_p95, normalized_seam_length(총 seam 길이 / bbox diagonal). HARD: zero-area 0, `min_island_uv_area`(1e-4) 미만 비면제 island 0, sliver(PCA aspect ≥ 8 이고 uv_area ≤ 0.01) 비면제 0. 면제: island 경계가 전부 mandatory fold/boundary/non-manifold 인 `mandatory_bounded`(report 에 `exempt_islands` 기록).
- merge-back: 인접 island 쌍의 공유 seam 그룹을 길이 내림차순으로 하나씩 제거 시험, 전체 측정 통과 + island 1 감소 시에만 채택, 그 외 스냅샷 복원. 모든 accepted fixture 가 `complete = true, reason = no_removable_seam, removable_remaining = 0` 으로 종료(bevel_cube 4회·torus 3회·cylinder 1회·uv_sphere 1회 시험, 전부 품질 사유로 거절 → 제거 가능 seam 0). 시험 예산 `merge_back_max_trials = 64`.
- baseline 비교: island 수 증가 0(082f70e 와 동일).

## G8 — Texel density

- density = sqrt(uv_area/area_3d)·texture_size; 3D 면적 가중 CV ≤ 0.15, island 상대 밀도 이탈(±0.30) 0. 균일 scale 불변성은 단위 테스트(1e-9). fixture cv 최대 2.1e-3(suzanne), 실모델 2.6e-5, outlier 0.
- intentional weighting 은 metadata 인자로만 받고 엔진은 추정하지 않는다(`intentional_weighting_applied` 기록).

## G9 — Pixel padding

- island-to-island ≥ 4 px, island-to-border ≥ 4 px @1024 (pixel 환산). 패커가 타일 경계까지 채우면 `ensure_border_margin` 이 균일 similarity 변환(scale ≤ 1, 중심 이동)으로 inset 하고 `border_inset` 에 기록(왜곡/밀도 비율 불변). gap 실패는 재배치만(`repack_for_gap`, 최대 3회). export 재읽기 후에도 동일 검사(G13).

## G10 — Shading / tangent

- profile `shading_uv_policy` = preserve(기본): worker 가 엔진 전 `shading_snapshot`(sharp edge 집합·smooth flag 해시)을 찍고 엔진 후 비교, 모든 fixture/실모델 `unchanged`. `require_uv_seam_on_sharp_edges` 는 sharp edge 를 `SeamConstraints.required`(mandatory 와 같이 제거 불가, protected 보다 우선) 로 강제하고 UV 분리를 감사; `split_normals_on_uv_seams` 는 최종 seam 에 `apply_smoothing_split_by_edges` 적용 후 감사. 두 정책은 sharp-normal 경계 fixture 단위 테스트로만 검증(실 normal-map 에셋 없음).
- tangent basis: export 재읽기에서 `calc_tangents` 성공(`tangent_ok = true`, fbx/glb).

## G11 — Packing

- HARD(overlap/bounds/padding/density)는 G3/G8/G9 로 통과. QUALITY `packing_efficiency_min = 0.42` 는 `quality_report.sections.packing`(advisory) 로 보고만 하며 seam 추가 사유가 아니다.

## G12 — Determinism / budget

- bevel_cube·suzanne 3회: seam 집합 동일, float 지표 차 ≤ 1e-6, merge-back history(edges/accepted 순서)·removed_edges·island 수 동일. 예산 키 4개 + merge_back_max_trials 기록, 예산 종료 시 needs_user_review(suzanne `no_improving_candidate`, 실모델 `island_cap`).

## G13 — Export round trip

- bevel_cube accepted 결과를 FBX+GLB 로 export → 재읽기 audit 통과(각 4 island, mandatory unsplit 0, correctness/padding/texel/shading 통과, UV corner fingerprint hash 일치, vertex fingerprint 일치, topology 일치, tangent ok). GLB 는 importer 가 법선이 다른 정점을 합치지 않으므로 위치 기준 weld(216 → 56 정점) 후 감사한다. export status `accepted`.
- 거부 경로: suzanne needs_user_review 결과 export → fbx `correctness_failed`, glb `correctness_failed, mandatory_90_uv_unsplit` → 두 포맷 모두 `reread_audit_failed`, export status `failed`, manifest 미작성(`uv_export_refused.json`).

## G14 — Status / artifact atomicity

- accepted/needs_user_review/failed 분류에 fragmentation·texel·shading·merge-back·connectivity 실패 코드 추가(`evaluate_auto_gate`). staging 저장 → 재읽기 audit → `os.replace` 승격 → handoff sha256 비교는 기존과 동일. exit code 0 만으로 성공 판정하지 않음(needs_user_review 도 exit 0).

## G15 — Evidence

- run 디렉터리: `selected_uv_layout.png`(=uv_layout), `selected_checker_front/side.png`, `selected_heatmap_anisotropy.png`(=distortion_heatmap), `seam_overlay.json` + `seam_overlay.png`, `quality_report.json`, `candidate_history.json`, `merge_back_history.json`, `shading_policy.json`, export 디렉터리 `export_reread_report.json`. 기존 파일명은 유지하고 새 파일을 추가했다(대응은 괄호).
- overlay `reason_code` 7종(mandatory_90 / boundary_topology / user / shading / material / distortion_added / rejected_candidate) 과 색 범례, 클릭 시 cut_reason·cost·improvement·target island. UI: Game gates 섹션, merge-back 목록, rejected dashed layer, seam_overlay.png 토글(`app/test` 42건 통과). GUI 수동 walkthrough 는 BLOCKED.

## G16 / G17 — Calibration / holdout (BLOCKED)

calibration set 과 리뷰어 승인/거절 UV 쌍이 저장소·세션에 없다. 실프로젝트 low-poly 1건(humanstatue, 승인된 `work/working_lowpoly.blend`, 5,996 정점 / 11,776 면) 을 최종 코드로 headless 실행한 결과를 제외하지 않고 기록한다:

| 항목 | 값 |
|---|---|
| 상태 | needs_user_review (quality_profile_failed, correctness_failed(overlap 0.00264), reread_audit_failed, fragmentation_failed) |
| 필수 seam | 90° fold 321, missing 0, uv_unsplit 0 (재읽기 후 0), mesh identity 불변, shading preserve 통과 |
| island | 27 (초기) → 80 (island cap); seam type: segmentation 925, welded_fold_auxiliary 168, mandatory_90 321, overlap_repair 91 |
| 왜곡 v2 | global anisotropy p95 1.95 / max 131, area stretch mean 0.205 / p95 0.744, bad area ratio 0.074; worst island anisotropy p95 6.12 |
| fragmentation | tiny 58, sliver 20(비면제 sliver hard 실패), below_min_area 33, one/two-face 26, aspect p95 36.6, normalized seam length 18.4 (auxiliary 16.1) |
| texel density | cv 2.6e-5, outlier 0 (통과); padding 통과(gap 4.88 px, border 4 px) |
| refinement / merge-back | island cap 에 먼저 도달해 refinement 0회; merge-back 은 품질 실패로 skip (removable group 86개 남음) |
| 성능 | 86.6 s, peak 377 MB |

해석: 데시메이트된 statue 는 fold 321 개와 welded-fold 보조 절개·overlap 수리로 island 가 cap 까지 늘어나고, 그 결과가 새 fragmentation hard 규칙(tiny/sliver)에 걸린다. 자동 규칙은 계획서대로 정직하게 needs_user_review 로 끝났다. calibration(G16) 에서 결정할 항목: (1) 저폴리 근사 fold 의 mandatory 취급, (2) welded-fold 보조 절개와 island cap 의 예산 배분, (3) tiny/sliver 기준값과 면제 규칙, (4) area stretch p95 / anisotropy 상한.

## 메인이 내린 설계 판단 (문서에 없던 항목)

1. profile 키는 Acceptance 문서 이름으로 rename 하고(예: `anisotropy_global_p95_max`, `bad_area_threshold`, `bad_area_ratio_max`), 신규 키(area stretch p95 상한, border_margin_px, tiny/sliver/aspect, texel density, packing floor, shading_uv_policy, merge-back 예산)를 engineering_v0 에 추가했다. margin_px 는 8 이 아닌 기존 4 를 유지(보정 전 동작 변경 회피).
2. v2 report 의 내부 필드명(`exceed_area_fraction` 등)은 유지하고 G4 필수 이름은 `summary` 블록으로 노출. 계획서 §5 의 `angle_distortion_p95` 는 Acceptance 문서에 없어 추가하지 않았다(문서가 단일 기준).
3. fragmentation/texel/shading 은 `evaluation.py` 대신 새 순수 모듈(`uv_agent/geometry/fragmentation.py`, `texel_density.py`, `shading_policy.py`)에 두었다.
4. sliver 정의: PCA 정렬 bbox aspect ≥ 8 이고 uv_area ≤ 0.01; tiny: uv_area < 0.002; hard 최소 면적 1e-4. 면제 `mandatory_bounded` 만 구현(detail/cap 분류 metadata 없음).
5. texel density 는 sqrt(uv/3D 면적)·texture_size, 3D 면적 가중 평균/CV.
6. border padding 실패는 절개 대신 균일 inset 변환으로 복구(`ensure_border_margin`, measure 시점에 적용·기록).
7. merge-back 은 전체 측정이 통과한 레이아웃에서만 실행하고(실패 시 `skipped_quality_failed`), gap 단독 실패는 먼저 재배치한 뒤 실행한다.
8. 후보 cost breakdown 은 기록·tie-break 용이며 lexicographic 순서(island → seam length → visible → total cost)를 바꾸지 않는다. `concave_crease_bonus` 는 MeshGraph 에 부호 있는 dihedral 이 없어 crease bonus 로 계산(키 이름 유지).
9. G2 의 "이미 통과한 island" 는 전체 profile 통과로 해석: global 만 실패하면 최악 island 를 대상으로 삼는다.
10. `islands_disagree` 는 보고에서 hard gate(`island_connectivity_mismatch`)로 승격.
11. shading 정책 `preserve` 의 before snapshot 은 worker 가 엔진 전에 찍는다; `require_uv_seam_on_sharp_edges` 의 sharp edge 는 mandatory 와 별도의 `required` 집합으로 추적(protected 보다 우선, 제거 불가, 비용 0).
12. export 재읽기: glTF 는 `merge_vertices=True` import 후 위치 기준 weld(1e-6); UV corner fingerprint 는 5자리 해시 우선, 불일치 시 정렬 배열 allclose(5e-4) 로 float32 왕복 오차 흡수. 재읽기 audit 실패 포맷은 succeeded 에서 제외.
13. evidence 파일명은 기존 이름을 유지하고 새 이름(`quality_report.json`, `merge_back_history.json`, `seam_overlay.png`, `shading_policy.json`, `export_reread_report.json`)을 추가했다.
14. e2e export round trip 은 accepted fixture(bevel_cube)로 수행하고, 비승인 결과(suzanne)의 export 거부를 별도 테스트로 증거화했다.
