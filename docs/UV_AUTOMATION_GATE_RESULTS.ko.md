# 자동 UV Acceptance Gate 실행 결과

- 대응 문서: [Acceptance Gate](UV_AUTOMATION_ACCEPTANCE_GATES.ko.md), [작업 계획서](UV_AUTOMATION_WORK_PLAN.ko.md)
- 판정 기준 코드: `082f70e` (main)
- 실행 환경: Windows 10 Home 10.0.19045 x64, Python 3.14.4 (uv), Node/npm 11, Blender 5.1.2 (build hash ec6e62d40fa9, 2026-05-19), electron-builder 24.13.3
- 증거 경로: `tests/e2e/*` 실행 결과 JSON(저장소 `docs/evidence/uv_automation_082f70e/`), 본 문서의 표. 실모델(human statue 등)과 리뷰어는 이 세션에 없다.
- 판정은 메인이 최종 코드 기준 raw 출력을 직접 대조해 내렸다. 구현 완료와 실행 검증, solver accepted 와 아티스트 승인을 구분한다.

## 요약 표

| Gate | 상태 | 코드/설정 버전 | 증거 경로 | 실패 이유/다음 조치 |
|---|---|---|---|---|
| G0 | PASS (synthetic) / BLOCKED (실모델) | 082f70e, profile engineering_v0 | `tests/e2e/test_fixture_models.py`, `run_manifest.json`, `mesh_identity.json` | human statue 원본/기존 UV 없음 → 실모델 항목 BLOCKED |
| G1 | PASS | 082f70e | `tests/test_mesh_graph_v2.py`, `tests/test_uv_correctness.py`, `tests/e2e/test_uv_auto_gates.py` | — |
| G2 | PASS | 082f70e | `tests/test_uv_generate_mode_contract.py`, `app/test/uv-generate-contract.test.ts`, `app/test/integration.test.ts`, `tests/e2e/test_uv_auto_gates.py` | — |
| G3 | PASS | 082f70e, metric_version 2 | `tests/test_distortion_v2.py`, `tests/test_quality_profile.py` | — |
| G4 | PASS | 082f70e | `tests/test_seam_candidates.py`, `tests/test_segmentation_constraints.py`, `tests/test_pipeline_auto_integration.py`, e2e bevel_cube/protected_path | — |
| G5 | PASS | 082f70e | `tests/test_refinement_loop.py`, `tests/test_pipeline_auto_integration.py`, e2e 결정성 3회 | — |
| G6 | PASS | 082f70e | `app/test/integration.test.ts`(G6 테스트), worker staging/handoff, e2e degenerate_input/suzanne | — |
| G7 | PASS (데이터·IPC·UI 구현) / BLOCKED (GUI walkthrough) | 082f70e | `app/test/integration.test.ts`(approval/feedback/run view), e2e feedback fingerprint 테스트, `seam_overlay.json`, heatmap PNG | 렌더러 GUI 수동 walkthrough 자동화 불가 → 사용자 확인 필요 |
| G8 | BLOCKED | engineering_v0 (calibrated=false) | `uv_performance.json` (성능 3회) | 실모델·승인/거절 UV 쌍·리뷰어 부재. calibration 전 자동 품질 출시 BLOCKED |
| G9 | PASS (명령 세트·installer·Blender 실행·export 재읽기·한글 경로) / BLOCKED (packaged GUI E2E, sample FBX 기반 기존 e2e) | 082f70e | `npm run typecheck/test:integration/build/dist:win`, `tests/e2e/test_uv_auto_export_perf.py`, `tests/e2e/test_uv_auto_gates.py::test_korean_space_path` | packaged 앱 GUI 시나리오는 수동 확인 필요; sample/*.fbx 기반 MVP e2e 는 자산 부재로 skip |

## G0 — 재현 가능한 기준선

- fixture manifest: `tests/e2e/fixtures/build_fixture_models.py` 가 11개 fixture(plane_grid, cube, bevel_cube, cylinder_capped, uv_sphere, torus, suzanne, concave_plate, angle_boundary, degenerate_input, protected_path)를 Blender 5.1.2 로 생성하고 파일 SHA-256, vertex/edge/face/loop 수, topology-canonical fingerprint, Blender build 를 기록. 두 번 빌드 시 fingerprint 동일(`test_fixture_build_is_deterministic`).
- run manifest: 각 run 의 `run_manifest.json` 에 commit SHA, platform, Blender/Python 버전, 모델 SHA-256, mesh fingerprint, mode, seed, 옵션, profile 기록.
- mesh identity: UV 작업 전후 fingerprint/카운트 비교 `mesh_identity.unchanged == True` (11/11 fixture).
- 기존 실험 문서 수치는 사용하지 않았다. human statue 원본/기존 UV 는 저장소에 없어 실모델 항목 BLOCKED.
- 발견: Blender 5.1.2 는 동일 primitive 생성에서도 polygon 저장 순서가 실행마다 달라진다 → fingerprint 는 면 순서에 불변하도록 정규화했고, 재읽기 audit 은 edge id 가 아닌 기하 키로 대응한다.

## G1 — 필수 seam 과 최종 UV correctness

| 항목 | 결과 | 증거 |
|---|---|---|
| 각도 경계 | 89.9 비필수, 90.0/90.1 필수; \|angle−90\| ≤ 1e-5 만 snap | `tests/test_mesh_graph_v2.py`, e2e `test_angle_boundary_rule` (seam_type_counts mandatory_90 = 2, 89.9 fold 는 seam 미포함) |
| 필수 seam 집합 / UV 분리 | 자동 실행 10/10 정상 fixture 에서 mandatory_90_missing = 0, mandatory_90_uv_unsplit = 0; 저장 파일 재읽기 후에도 0 (`final_reread_audit`) | `test_auto_generate_all_fixtures` |
| 뒤집힘/퇴화 | 정확 삼각형 교차 면적·island 다수결 orientation·UV 퇴화 audit; 실패 시 accepted 금지 | `tests/test_uv_correctness.py`, suzanne needs_user_review |
| 범위 | [0,1] ± 1e-4, NaN → 실패 | `bounds_audit` 테스트 |
| 겹침 | 교차 면적 합 ≤ 1e-8, 경계 접촉과 구분 | `test_touching_islands...` |
| packing 간격 | pack margin = max(margin, margin_px/texture_size); gap 실패 시 재배치만(절개 0) | `test_pack_margin_meets_profile_margin_px`, `test_island_gap_failure_is_repacked_never_recut` |
| topology/입력 | non-manifold/면적 0/입력 퇴화 삼각형 진단 → `input_defects`, degenerate_input 은 needs_user_review | e2e degenerate_input |
| edge ID 변경 | 재읽기 시 기하 키 대응표로 seam 집합 재검사 | `uv_agent/geometry/mesh_identity.py`, worker `_final_reread_audit` |

## G2 — 실행 모드와 호환성

- UV/spec 없는 cube + auto_generate → 자동 solver 실행, needs_input 아님 (e2e a). preserve_existing + spec/UV 없음 → needs_input (e2e b).
- preserve round trip: auto 결과 seam 집합을 spec 으로 저장 후 preserve 실행 → 대칭차 0, auto_added_seams 0, `mandatory_audit.reported_only = true` (cube, suzanne).
- mode 없는 프로젝트 → preserve_existing (통합 테스트 legacy project). UI → IPC → job.json → status/summary → project 에 같은 mode 기록.
- 모순 플래그: TS/Python 양쪽 `validateModeRequest`; 실행 전 오류 (통합 테스트 + e2e contradictory_flags exit 2).
- 자동 모드는 strict integrity 대신 `evaluate_auto_constraints`/`evaluate_auto_gate` 로 판정.

## G3 — 왜곡 지표 v2

`tests/test_distortion_v2.py` (G3 표 허용 오차 그대로): 등각 = 1 ± 1e-6, 균일 scale/rot/trans 변화 ≤ 1e-6, ×4/×0.25 = 16 ± 1e-6, area-preserving shear 면적 왜곡 ≈ 0 & anisotropy 해석값 일치, 국소 collapse → uv_degenerate 카운트 + profile 실패(0점 통과 없음), tiny bad patch max/island/region 노출, 오목 n-gon 실제 triangulation(phantom 없음, fan 대조), mean/p95 면적 가중 독립 재계산, NaN → invalid. `metric_version = 2` 없이는 v1/v2 를 섞지 않는다(profile 검사).

## G4 — 절개 경로와 제약

- `SeamConstraints`: mandatory > locked > protected > preferred; 충돌은 기록(mandatory_wins). 모든 호출 경로(초기 segmentation, flipped/raster split, welded-fold repair, correctness_pass, merge/absorb/straighten, prune, refinement) 에 동일 제약. 보호 edge 를 자르는 후보는 통째로 거절되고 기록된다.
- 후보: unwrap-only, short cut(고왜곡 영역→경계 2-leg 경로, 보호 우회), preferred/exposure 경로(front_axis 없으면 visibility neutral), normal split. 품질 동등 시 island 수 → 보조 seam 길이 → 노출 비용.
- e2e: locked 5개 유지/locked_missing 0, protected 미절개, bevel_cube 는 2 round·6 후보 평가 후 accepted (실제 Blender).

## G5 — 후보 채택·복원과 종료

- `accept_candidate`: correctness/제약/회귀 예산 위반 무조건 거절 → 품질 통과 또는 개선율 ≥ 0.15. 예산 누락 지표는 0.
- 스냅샷 복원: 개선 없음/악화/예외 후보 주입 후 seam 집합·UV 배열 동일 (`test_refinement_loop.py`).
- 결정성: fake backend 3회 동일; 실제 Blender suzanne/bevel_cube 3회 seam 집합 동일, float 차 ≤ 1e-6.
- 예산 종료: max_iterations/time_budget/island_cap 각각 termination reason 기록; 품질 미달 → needs_user_review.
- packing/convexity 단독 문제로 절개 0 (gap 은 재배치만).

## G6 — 상태와 승인 파일

- 통합 테스트: needs_user_review/failed/cancel 후 이전 `work/uv/selected_uv.blend` 해시 불변, 포인터 유지. accepted 는 파일 실존 시에만 포인터 교체.
- worker: `.staging/` 저장 → 재읽기 audit → `os.replace` 승격 → handoff `.tmp` + sha256 비교 → `finalize_acceptance` (artifact/handoff/identity 실패 시 needs_user_review).
- NaN/누락 지표 → `valid=false`, accepted 금지 (`evaluate_auto_gate`, `evaluate_quality`).
- preserve 성공은 "seam 유지 성공" 으로 표시(`mandatory_audit.reported_only`), 자동 규칙 통과로 표시하지 않음.

## G7 — 리뷰와 사용자 피드백

- `seam_overlay.json`(type: mandatory_90/user_seam/locked/segmentation/welded_fold_auxiliary/overlap_repair/distortion_split/correctness_repair + reason/round/target/improvement, 3D 좌표), `selected_heatmap_anisotropy.png`(최종 UV 위 실제 triangulation 래스터), `candidate_history.json`, global/island v2 표, 보조 seam 길이·정규화 길이.
- 피드백: `work/uv/uv_feedback.json` 에 lock/protect/prefer/front_axis + mesh_fingerprint 저장 → 다음 run 에서 fingerprint 일치 시만 적용(e2e: fingerprint_match → locked edge 최종 seam 포함; 'deadbeef' → fingerprint_mismatch 미적용).
- artist approval 은 project.json 에 별도 저장(거절 사유 필수, run_id 고정); summary 의 solver_accepted 와 분리.
- GUI walkthrough(3D overlay 클릭 등)는 자동화 하네스가 없어 BLOCKED(수동 확인 필요). typecheck/build 는 통과.

## G8 — 실모델 품질과 임계값 고정 (BLOCKED)

- 실모델(hard-surface/organic/elongated)과 리뷰어 승인/거절 UV 쌍이 저장소/세션에 없다. calibration/holdout 미수행.
- profile `engineering_v0` 는 필수 키를 모두 갖되 `calibrated=false`. anisotropy 상한(1.6/1.8/3.0)은 공학 초기값이며 기존 area stretch 0.50/0.60 을 복사하지 않았다. 자동 품질 출시는 BLOCKED.
- 성능(고정 하드웨어, 3회): 

| fixture | wall_s median/max | elapsed_s median/max | peak_memory_mb median/max | status(3회) |
|---|---|---|---|---|
| suzanne | 26.814 / 27.242 | 24.706 / 25.134 | 238.99 / 239.0 | needs_user_review ×3 |
| torus | 14.724 / 15.451 | 12.212 / 13.117 | 233.13 / 233.22 | accepted ×3 |

하드웨어: Windows 10 10.0.19045, AMD64 12 core, 49,078 MB RAM, Blender 5.1.2 ec6e62d40fa9. 목표 시간 예산은 calibration 시 고정 예정이므로 미설정.

## G9 — Windows 패키지와 전체 회귀

- 명령 세트(최종 코드): `uv run python -m pytest tests --ignore=tests/e2e` exit 0 (skip 3건, 아래); `npm run typecheck` exit 0; `npm run test:integration` exit 0, tests 38 / pass 38 / fail 0; `npm run build` exit 0; `npm run dist:win` exit 0 → `app/release/Reforge Setup 0.1.0.exe` (81,868,894 bytes, SHA256 A6B9A0A07263F5D5FB432EBA8FA5B96C218032CC1110922FAE21D00823191D67).
- dist:win 환경 우회: electron-builder 의 winCodeSign 7z 에 darwin 심볼릭 링크가 있어 심볼릭 링크 권한 없는 계정에서 추출이 실패한다. 캐시 디렉터리에 `winCodeSign-2.6.0` 을 미리 풀어 두면 빌드가 성공한다(저장소 변경 없음). 개발자 모드/관리자 권한 없이 재현하려면 같은 준비가 필요하다.
- Blender 경로 탐지: 버전별 Windows 설치 디렉터리 최신 우선; 지원 버전 미만/판독 불가 → run 생성 전 오류(`blender-version.test.ts`).
- 한글·공백 경로에서 auto 실행 accepted (e2e). export(fbx/glb) 후 재읽기: UV layer 1개, unsplit 0(원본 topology edge 기준), bounds OK.
- 기존 UV/seam/low-poly 테스트: 단위 스위트 전부 통과. skip: `tests/test_obj_loader.py` 2건(sample/uv_no.obj 부재, G9 증거만 영향), `tests/test_uv_review_artifacts.py` 1건(구식 Blender 탐지가 Windows 경로 미지원 → skip; G9 영향), MVP e2e 는 sample FBX 부재로 skip.
- packaged 앱 GUI 시나리오(high-poly → low-poly → 자동 UV → 리뷰 → export)는 GUI 자동화 하네스가 없어 BLOCKED. 동일 흐름은 worker/main-process 통합 테스트로 검증했다.

## 메인이 내린 설계 판단 (문서에 없던 항목)

1. 90도 snap 을 MeshGraph 생성 시점에 적용(모든 비교가 자동으로 일관).
2. Face.triangles 로 실제 triangulation 보관(Blender loop triangles / ear clipping fallback); v1 지표는 의미 유지.
3. profile `engineering_v0` 의 anisotropy 초기값(p95 1.6 global / 1.8 island, max 3.0, exceed basis 1.6, 초과면적 5%/10%) — 미보정으로 명시.
4. orientation 정책: island 다수결 부호; 국소 반대 부호 = 실패, island 전체 반사 = 보고.
5. auto 모드 옵션 기본값은 strict 플래그 4개 true; preserve 는 false. raw 옵션의 모순만 오류.
6. auto 모드에서 spec 의 user seam 은 lock, protected 는 forbidden, preferred 는 soft cost; uv_layer 는 사용하지 않음.
7. export 는 선택 UV layer 만 출하(다른 layer 제거, 메모리 내).
8. 입력 결함(non-manifold/면적 0/퇴화/고립 정점)은 auto 모드에서 `input_defects` 로 accepted 금지.

## 최종 실행 결과 표 (auto_generate, 기본 옵션, Blender 5.1.2, HEAD 082f70e)

| fixture | status | auto_gate.failures | termination (reason / iter / cand) | islands | elapsed_s |
|---|---|---|---|---|---|
| plane_grid | accepted | — | quality_passed / 0 / 0 | 1 | 0.57 |
| cube | accepted | — | quality_passed / 0 / 0 | 6 | 8.26 |
| bevel_cube | accepted | — | quality_passed / 2 / 6 | 4 | 26.67 |
| cylinder_capped | accepted | — | quality_passed / 0 / 0 | 4 | 11.07 |
| uv_sphere | accepted | — | quality_passed / 0 / 0 | 2 | 4.86 |
| torus | accepted | — | quality_passed / 0 / 0 | 3 | 11.55 |
| suzanne | needs_user_review | quality_profile_failed, correctness_failed(overlap, orientation, island_gap), reread_audit_failed | no_improving_candidate / 1 / 3 | 12 | 36.43 |
| concave_plate | accepted | — | quality_passed / 0 / 0 | 10 | 13.48 |
| angle_boundary | accepted | — | quality_passed / 0 / 0 | 5 | 10.40 |
| degenerate_input | needs_user_review | input_defects | quality_passed / 0 / 0 | 10 | 35.10 |
| protected_path | accepted | — | quality_passed / 0 / 0 | 1 | 0.96 |

실패 사례를 제외하지 않는다: suzanne 은 자동 규칙(미보정 profile 기준)을 통과하지 못해 needs_user_review 로 정직하게 종료되며, 이는 G6 의 요구 동작이다. degenerate_input 은 입력 결함 진단으로 accepted 가 금지된다.

export 재읽기(suzanne, needs_user_review 결과의 run 디렉터리 selected_uv.blend 를 입력): fbx UV layer ["AI_UV"], 원본 topology edge 기준 unsplit 0/76, bounds OK; glb UV layer 1개, 삼각화 대각선 2개 제외 후 unsplit 0, bounds OK; export status accepted.
