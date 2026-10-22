# qdcheck 突变测试：历次漏网逐条判定（triage）

由 `python3.12 -m qdcheck.tools.triage --replay` 生成：收集 `results/mutation_history/*.json` 每一轮的全部漏网
（去重后 162 处突变），在**当前**源码里找同一处突变并用**最终**测试重跑一次。
“抓住”列出第一个失败的测试；“仍漏网”的每条都有等价判定理由（`results/mutation_triage.json`）；
“代码已不存在”表示那段代码在后续修改中被删除或改写（所在行与改动仍列出）。

最终全量结果：732/780 = 93.8%（`results/mutation.json`）。重跑耗时 517.6 s。
重跑 158 处：抓住 127，仍漏网 31（全部有判定），代码已不存在 4。


| 模块 | 行 | 首次漏网于 | 改动 | 现在 | 说明 |
|---|---:|---|---|---|---|
| `compare` | 58 | round1 | `0.0` → `1.0` | 抓住 | 抓住：`FAIL: test_helpers (qdcheck.tests.test_compare.TestCompareSeries.test_helpers)` |
| `compare` | 72 | round1 | `rel_tol * scale >= abs_tol` → `rel_tol * scale > abs_tol` | 抓住 | 抓住：`FAIL: test_error_statistics (qdcheck.tests.test_compare.TestCompareSeries.test_error_stati` |
| `compare` | 77 | round1 | `diff / allowed` → `diff * allowed` | 抓住 | 抓住：`FAIL: test_file_level_statistics (qdcheck.tests.test_compare.TestCompareFile.test_file_lev` |
| `compare` | 145 | round1 | `0` → `1` | 抓住 | 抓住：`FAIL: test_cli_exit_codes_and_first_line_contract (qdcheck.tests.test_compare.TestCompareF` |
| `determinism` | 75 | round2 | `600.0` → `900.0` | 抓住 | 抓住：`FAIL: test_more_defaults (qdcheck.tests.test_determinism.TestMoreDetails.test_more_default` |
| `determinism` | 88 | round2 | `3000` → `3001` | 抓住 | 抓住：`FAIL: test_child_stderr_tail_is_3000_chars (qdcheck.tests.test_determinism.TestMoreDetails` |
| `determinism` | 92 | round1 | `time.perf_counter() - t0` → `time.perf_counter() + t0` | 抓住 | 抓住：`FAIL: test_timing_fields_with_fake_clock (qdcheck.tests.test_determinism.TestReportDetails` |
| `determinism` | 92 | round1 | `3` → `4` | 抓住 | 抓住：`FAIL: test_timing_fields_with_fake_clock (qdcheck.tests.test_determinism.TestReportDetails` |
| `determinism` | 131 | round1 | `len(text) <= 200` → `len(text) < 200` | 抓住 | 抓住：`FAIL: test_short_values_and_key_lists (qdcheck.tests.test_determinism.TestReportDetails.te` |
| `determinism` | 134 | round2 | `0.0` → `1.0` | 抓住 | 抓住：`FAIL: test_more_defaults (qdcheck.tests.test_determinism.TestMoreDetails.test_more_default` |
| `determinism` | 135 | round1 | `math.isnan(a) or math.isnan(b)` → `math.isnan(a) and math.isnan(b)` | 漏网 | **仍漏网 → 未判定**：（缺判定） |
| `determinism` | 142 | round1 | `1e-09` → `1.5000000000000002e-09` | 抓住 | 抓住：`FAIL: test_more_defaults (qdcheck.tests.test_determinism.TestMoreDetails.test_more_default` |
| `determinism` | 151 | round2 | `continue` → `break` | 抓住 | 抓住：`FAIL: test_lists_and_dicts (qdcheck.tests.test_determinism.TestDiff.test_lists_and_dicts)` |
| `determinism` | 164 | round1 | `20` → `21` | 抓住 | 抓住：`FAIL: test_short_values_and_key_lists (qdcheck.tests.test_determinism.TestReportDetails.te` |
| `determinism` | 186 | round1 | `4242` → `4243` | 抓住 | 抓住：`FAIL: test_deterministic_engine_passes (qdcheck.tests.test_determinism.TestFakeEngines.tes` |
| `determinism` | 187 | round1 | `True` → `False` | 抓住 | 抓住：`FAIL: test_deterministic_engine_passes (qdcheck.tests.test_determinism.TestFakeEngines.tes` |
| `examples` | 21 | round1 | `16` → `17` | 漏网 | **仍漏网 → 未判定**：（缺判定） |
| `examples` | 22 | round1 | `500` → `501` | 抓住 | 抓住：`FAIL: test_good_validate_has_no_bare_exception_and_matches_reference (qdcheck.tests.test_f` |
| `examples` | 89 | round1 | `out['side'] = side` → `pass` | 抓住 | 抓住：`FAIL: test_normalised_output (qdcheck.tests.test_examples.TestValidates.test_normalised_ou` |
| `examples` | 106 | round1 | `0.5` → `0.75` | 抓住 | 抓住：`FAIL: test_boundaries_shared_by_all (qdcheck.tests.test_examples.TestValidates.test_bounda` |
| `examples` | 117 | round1 | `return _rest(cfg, out)` → `return None` | 抓住 | 抓住：`FAIL: test_boundaries_shared_by_all (qdcheck.tests.test_examples.TestValidates.test_bounda` |
| `examples` | 130 | round1 | `raise ValidationError('config must be an object')` → `pass` | 抓住 | 抓住：`FAIL: test_the_designed_bugs_and_only_those (qdcheck.tests.test_examples.TestValidates.tes` |
| `examples` | 132 | round1 | `period > 500` → `period >= 500` | 抓住 | 抓住：`FAIL: test_boundaries_shared_by_all (qdcheck.tests.test_examples.TestValidates.test_bounda` |
| `examples` | 136 | round1 | `out = {'period': period, 'bars_per_period': bars_per_period,` → `pass` | 抓住 | 抓住：`FAIL: test_boundaries_shared_by_all (qdcheck.tests.test_examples.TestValidates.test_bounda` |
| `examples` | 141 | round1 | `0` → `-1` | 抓住 | 抓住：`ERROR: test_the_designed_bugs_and_only_those (qdcheck.tests.test_examples.TestValidates.te` |
| `examples` | 143 | round1 | `True` → `False` | 抓住 | 抓住：`FAIL: test_boundaries_shared_by_all (qdcheck.tests.test_examples.TestValidates.test_bounda` |
| `examples` | 144 | round1 | `i + 1` → `i - 1` | 抓住 | 抓住：`FAIL: test_boundaries_shared_by_all (qdcheck.tests.test_examples.TestValidates.test_bounda` |
| `examples` | 178 | round1 | `1` → `0` | 抓住 | 抓住：`FAIL: test_trades_replay_exactly (qdcheck.tests.test_examples.TestToyEngines.test_trades_r` |
| `examples` | 178 | round1 | `1` → `2` | 漏网 | **仍漏网 → 未判定**：（缺判定） |
| `examples` | 191 | round1 | `-held[s]` → `held[s]` | 抓住 | 抓住：`FAIL: test_trades_replay_exactly (qdcheck.tests.test_examples.TestToyEngines.test_trades_r` |
| `examples` | 192 | round1 | `held[s] = 0` → `pass` | 抓住 | 抓住：`FAIL: test_trades_replay_exactly (qdcheck.tests.test_examples.TestToyEngines.test_trades_r` |
| `examples` | 210 | round1 | `tr['price'] *= 1.0 + eps` → `tr['price'] /= 1.0 + eps` | 抓住 | 抓住：`FAIL: test_noisy_engine_factor (qdcheck.tests.test_examples.TestToyEngines.test_noisy_engi` |
| `examples` | 237 | round1 | `50` → `49` | 抓住 | 抓住：`FAIL: test_qbt_adapter (qdcheck.tests.test_examples.TestToyEngines.test_qbt_adapter)` |
| `fuzz` | 111 | round1 | `1` → `0` | 抓住 | 抓住：`FAIL: test_good_validate_has_no_bare_exception_and_matches_reference (qdcheck.tests.test_f` |
| `fuzz` | 111 | round1 | `1` → `2` | 漏网 | **仍漏网 → 未判定**：（缺判定） |
| `fuzz` | 154 | round1 | `1000` → `1001` | 漏网 | **仍漏网 → 未判定**：（缺判定） |
| `fuzz` | 160 | round5_merged | `0.5` → `0.75` | 抓住 | 抓住：`FAIL: test_round5_details (qdcheck.tests.test_fuzz.TestGenerators.test_round5_details)` |
| `fuzz` | 190 | round2 | `12` → `11` | 抓住 | 抓住：`FAIL: test_small_helpers (qdcheck.tests.test_fuzz.TestGenerators.test_small_helpers)` |
| `fuzz` | 196 | round3_merged | `continue` → `break` | 漏网 | **仍漏网 → 未判定**：（缺判定） |
| `fuzz` | 271 | round3_merged | `break` → `continue` | 漏网 | **仍漏网 → 未判定**：（缺判定） |
| `fuzz` | 271 | round3_merged | `break` → `pass` | 漏网 | **仍漏网 → 未判定**：（缺判定） |
| `fuzz` | 296 | round1 | `return rnd.choice(pool)` → `return None` | 抓住 | 抓住：`FAIL: test_every_schema_key_is_mutated_in_at_least_4_ways (qdcheck.tests.test_fuzz.TestCon` |
| `fuzz` | 301 | round1 | `2` → `1` | 抓住 | 抓住：`FAIL: test_small_helpers (qdcheck.tests.test_fuzz.TestGenerators.test_small_helpers)` |
| `fuzz` | 314 | round1 | `1e-09` → `1.5000000000000002e-09` | 抓住 | 抓住：`FAIL: test_boundary_values_exact (qdcheck.tests.test_fuzz.TestGenerators.test_boundary_val` |
| `fuzz` | 326 | round1 | `rnd.random() < 0.25` → `rnd.random() <= 0.25` | 漏网 | **仍漏网 → 未判定**：（缺判定） |
| `fuzz` | 336 | round1 | `cat == 'type_wrong'` → `cat != 'type_wrong'` | 抓住 | 抓住：`FAIL: test_recursion_on_long_string (qdcheck.tests.test_fuzz.TestBrokenValidatesAreCaught.` |
| `fuzz` | 350 | round1 | `-10 ** 6` → `10 ** 6` | 抓住 | 抓住：`FAIL: test_value_for_each_category (qdcheck.tests.test_fuzz.TestGenerators.test_value_for_` |
| `fuzz` | 358 | round1 | `if cat == 'unicode':
    return rnd.choice(UNICODE_SAMPLES)` → `if not cat == 'unicode':
    return rnd.` | 抓住 | 抓住：`ERROR: test_value_for_each_category (qdcheck.tests.test_fuzz.TestGenerators.test_value_for` |
| `fuzz` | 387 | round3_merged | `rnd.random() < 0.3` → `rnd.random() <= 0.3` | 漏网 | **仍漏网 → 未判定**：（缺判定） |
| `fuzz` | 409 | round5_merged | `0.05` → `0.07500000000000001` | 抓住 | 抓住：`FAIL: test_case_shape_distribution (qdcheck.tests.test_fuzz.TestGenerators.test_case_shape` |
| `fuzz` | 409 | round3_merged | `rnd.random() < 0.05` → `rnd.random() <= 0.05` | 漏网 | **仍漏网 → 未判定**：（缺判定） |
| `fuzz` | 429 | round3_merged | `len(s) >= 4 * p` → `len(s) > 4 * p` | 漏网 | **仍漏网 → 未判定**：（缺判定） |
| `fuzz` | 431 | round1 | `0` → `-1` | 抓住 | 抓住：`FAIL: test_mutate_on_empty_and_non_dict (qdcheck.tests.test_fuzz.TestRunner.test_mutate_on` |
| `fuzz` | 431 | round2 | `0` → `1` | 抓住 | 抓住：`FAIL: test_mutate_on_empty_and_non_dict (qdcheck.tests.test_fuzz.TestRunner.test_mutate_on` |
| `fuzz` | 439 | round1 | `len(cur) == 1` → `len(cur) != 1` | 抓住 | 抓住：`FAIL: test_recursion_on_deep_nesting (qdcheck.tests.test_fuzz.TestBrokenValidatesAreCaught` |
| `fuzz` | 443 | round3_merged | `32` → `33` | 抓住 | 抓住：`FAIL: test_round3_details (qdcheck.tests.test_fuzz.TestGenerators.test_round3_details)` |
| `fuzz` | 452 | round1 | `v == 0` → `v != 0` | 抓住 | 抓住：`FAIL: test_encoding_boundaries (qdcheck.tests.test_fuzz.TestGenerators.test_encoding_bound` |
| `fuzz` | 459 | round1 | `64` → `65` | 抓住 | 抓住：`FAIL: test_encoding_boundaries (qdcheck.tests.test_fuzz.TestGenerators.test_encoding_bound` |
| `fuzz` | 568 | round2 | `todo.extend(zip(x, y))` → `pass` | 抓住 | 抓住：`FAIL: test_round3_details (qdcheck.tests.test_fuzz.TestGenerators.test_round3_details)` |
| `fuzz` | 575 | round1 | `True` → `False` | 抓住 | 抓住：`FAIL: test_cases_identical_across_hash_seeds (qdcheck.tests.test_fuzz.TestDeterminism.test` |
| `fuzz` | 660 | round2 | `self.calls >= self.budget` → `self.calls > self.budget` | 抓住 | 抓住：`FAIL: test_accepted_invalid_and_zero_budget (qdcheck.tests.test_fuzz.TestRunner.test_accep` |
| `fuzz` | 684 | round5_merged | `mid + 1` → `mid - 1` | 抓住 | 抓住：`FAIL: test_recursion_on_long_string (qdcheck.tests.test_fuzz.TestBrokenValidatesAreCaught.` |
| `fuzz` | 692 | round3_merged | `while lo < hi and self.calls < self.budget:
    mid = (lo + ` → `while not (lo < hi and self.calls < self` | 抓住 | 抓住：`FAIL: test_recursion_on_deep_nesting (qdcheck.tests.test_fuzz.TestBrokenValidatesAreCaught` |
| `fuzz` | 713 | round1 | `i + 1` → `i - 1` | 抓住 | 抓住：`FAIL: test_round3_details (qdcheck.tests.test_fuzz.TestGenerators.test_round3_details)` |
| `fuzz` | 741 | round2 | `changed \|= new is not obj` → `pass` | 抓住 | 抓住：`FAIL: test_pass_reports_string_only_change (qdcheck.tests.test_fuzz.TestGenerators.test_pa` |
| `fuzz` | 789 | round1 | `20000` → `20001` | 漏网 | **仍漏网 → 未判定**：（缺判定） |
| `fuzz` | 849 | round3_merged | `1.0` → `1.5` | 漏网 | **仍漏网 → 未判定**：（缺判定） |
| `fuzz` | 872 | round1 | `if depth > 40:
    return True` → `if not depth > 40:
    return True` | 抓住 | 抓住：`FAIL: test_garbage_shapes (qdcheck.tests.test_fuzz.TestGenerators.test_garbage_shapes)` |
| `fuzz` | 872 | round3_merged | `40` → `39` | 抓住 | 抓住：`FAIL: test_round3_details (qdcheck.tests.test_fuzz.TestGenerators.test_round3_details)` |
| `fuzz` | 875 | round5_merged | `todo.extend(((y, d + 1) for y in x.values()))` → `pass` | 抓住 | 抓住：`FAIL: test_round5_details (qdcheck.tests.test_fuzz.TestGenerators.test_round5_details)` |
| `fuzz` | 878 | round3_merged | `False` → `True` | 抓住 | 抓住：`FAIL: test_garbage_shapes (qdcheck.tests.test_fuzz.TestGenerators.test_garbage_shapes)` |
| `fuzz` | 919 | round2 | `print(f'  {g['exc_type']} x{g['count']} first case {g['first` → `pass` | 抓住 | 抓住：`FAIL: test_cli_and_report_file (qdcheck.tests.test_fuzz.TestRunner.test_cli_and_report_fil` |
| `gen_vectors` | 42 | round1 | `step > 0` → `step >= 0` | 漏网 | **仍漏网 → 未判定**：（缺判定） |
| `gen_vectors` | 42 | round1 | `0` → `-1` | 抓住 | 抓住：`FAIL: test_regenerates_within_tolerance (qdcheck.tests.test_vectors.TestCommittedVectors.t` |
| `gen_vectors` | 42 | round2 | `0` → `1` | 抓住 | 抓住：`FAIL: test_regenerates_within_tolerance (qdcheck.tests.test_vectors.TestCommittedVectors.t` |
| `gen_vectors` | 191 | round1 | `True` → `False` | 抓住 | 抓住：`ERROR: test_main_writes_and_checks (qdcheck.tests.test_vectors.TestWriterAndChecker.test_m` |
| `gen_vectors` | 248 | round1 | `2` → `1` | 抓住 | 抓住：`FAIL: test_helpers_and_timing (qdcheck.tests.test_vectors.TestWriterAndChecker.test_helper` |
| `gen_vectors` | 262 | round1 | `1` → `0` | 抓住 | 抓住：`FAIL: test_regenerates_within_tolerance (qdcheck.tests.test_vectors.TestCommittedVectors.t` |
| `gen_vectors` | 262 | round1 | `1` → `2` | 抓住 | 抓住：`FAIL: test_regenerates_within_tolerance (qdcheck.tests.test_vectors.TestCommittedVectors.t` |
| `gen_vectors` | 263 | round1 | `out['bars'] += len(vec['closes'])` → `pass` | 抓住 | 抓住：`FAIL: test_regenerates_within_tolerance (qdcheck.tests.test_vectors.TestCommittedVectors.t` |
| `gen_vectors` | 286 | round1 | `abs(a - b) <= rel_tol * max(abs(a), abs(b), 1e-300)` → `abs(a - b) < rel_tol * max(abs(a), abs(b` | 抓住 | 抓住：`FAIL: test_helpers_and_timing (qdcheck.tests.test_vectors.TestWriterAndChecker.test_helper` |
| `gen_vectors` | 303 | round1 | `20` → `21` | 抓住 | 抓住：`FAIL: test_main_writes_and_checks (qdcheck.tests.test_vectors.TestWriterAndChecker.test_ma` |
| `gen_vectors` | 304 | round1 | `print(line)` → `pass` | 抓住 | 抓住：`FAIL: test_main_writes_and_checks (qdcheck.tests.test_vectors.TestWriterAndChecker.test_ma` |
| `indicators_alt` | 20 | round1 | `0.0` → `1.0` | 抓住 | 抓住：`FAIL: test_extra_random_series_with_all_features (qdcheck.tests.test_indicators.TestCrossC` |
| `indicators_alt` | 111 | round1 | `ag + al` → `ag - al` | 抓住 | 抓住：`FAIL: test_alt_rsi_balanced_window_is_fifty (qdcheck.tests.test_indicators.TestCrossCheck.` |
| `indicators_ref` | 50 | round1 | `3` → `4` | 抓住 | 抓住：`FAIL: test_file_headers (qdcheck.tests.test_vectors.TestCommittedVectors.test_file_headers` |
| `indicators_ref` | 155 | round2 | `1` → `0` | 抓住 | 超时（计入抓住） |
| `indicators_ref` | 155 | round1 | `1` → `2` | 抓住 | 抓住：`FAIL: test_extra_random_series_with_all_features (qdcheck.tests.test_indicators.TestCrossC` |
| `indicators_ref` | 175 | round1 | `1.0` → `1.5` | 漏网 | **仍漏网 → 未判定**：（缺判定） |
| `indicators_ref` | 383 | round1 | `raise ValueError("warmup must be 'none' or 'nan'")` → `pass` | 抓住 | 抓住：`FAIL: test_bad_input_raises_value_error (qdcheck.tests.test_indicators.TestResolve.test_ba` |
| `ledger` | 45 | round1 | `0` → `-1` | 抓住 | 抓住：`ERROR: test_bad_records (qdcheck.tests.test_ledger.TestValidation.test_bad_records)` |
| `ledger` | 77 | round1 | `not isinstance(bar, int) or isinstance(bar, bool) or bar < 0` → `not isinstance(bar, int) and isinstance(` | 抓住 | 抓住：`FAIL: test_bar_must_be_a_non_negative_int_even_for_valid_trades (qdcheck.tests.test_ledger` |
| `ledger` | 171 | round1 | `p > self.rel_tol * self.avg` → `p >= self.rel_tol * self.avg` | 漏网 | **仍漏网 → 未判定**：（缺判定） |
| `ledger` | 188 | round1 | `closing * (p - self.avg) * _sign(Q)` → `closing * (p - self.avg) / _sign(Q)` | 漏网 | **仍漏网 → 未判定**：（缺判定） |
| `ledger` | 257 | round1 | `self.cash + self.qty * p` → `self.cash - self.qty * p` | 抓住 | 抓住：`FAIL: test_cross_bankrupt_warning_uses_remaining_position (qdcheck.tests.test_ledger.TestV` |
| `ledger` | 293 | round1 | `abs(q) * avg` → `abs(q) / avg` | 抓住 | 抓住：`FAIL: test_identity_checks_fire_on_corrupted_state (qdcheck.tests.test_ledger.TestViolatio` |
| `ledger_alt` | 14 | round1 | `1` → `0` | 抓住 | 抓住：`ERROR: test_alt_accepts_integer_sides_and_snaps_like_ledger (qdcheck.tests.test_ledger.Tes` |
| `ledger_alt` | 14 | round1 | `1` → `2` | 抓住 | 抓住：`ERROR: test_alt_accepts_integer_sides_and_snaps_like_ledger (qdcheck.tests.test_ledger.Tes` |
| `ledger_alt` | 14 | round1 | `0` → `-1` | 抓住 | 抓住：`ERROR: test_alt_accepts_integer_sides_and_snaps_like_ledger (qdcheck.tests.test_ledger.Tes` |
| `ledger_alt` | 14 | round1 | `0` → `1` | 抓住 | 抓住：`ERROR: test_alt_accepts_integer_sides_and_snaps_like_ledger (qdcheck.tests.test_ledger.Tes` |
| `ledger_alt` | 51 | round1 | `abs(x) <= 1e-12 * max(1.0, abs(ref))` → `abs(x) < 1e-12 * max(1.0, abs(ref))` | 漏网 | **仍漏网 → 未判定**：（缺判定） |
| `ledger_alt` | 98 | round1 | `q - self.size` → `q + self.size` | 漏网 | **仍漏网 → 未判定**：（缺判定） |
| `ledger_alt` | 141 | round1 | `self.cash.value() < -self.cfg['rel_tol'] * scale` → `self.cash.value() <= -self.cfg['rel_tol'` | 漏网 | **仍漏网 → 未判定**：（缺判定） |
| `ledger_alt` | 171 | round1 | `1.0` → `1.5` | 漏网 | **仍漏网 → 未判定**：（缺判定） |
| `props` | 29 | round1 | `1.0 + 300.0 * rnd()` → `1.0 - 300.0 * rnd()` | 抓住 | 抓住：`FAIL: test_tamper_shape (qdcheck.tests.test_indicators.TestCrossCheck.test_tamper_shape)` |
| `props` | 31 | round1 | `d['high'][i] = max(o, c) * (1.0 + 0.05 * rnd())` → `pass` | 抓住 | 抓住：`FAIL: test_tamper_shape (qdcheck.tests.test_indicators.TestCrossCheck.test_tamper_shape)` |
| `props` | 31 | round1 | `0.05 * rnd()` → `0.05 / rnd()` | 抓住 | 抓住：`FAIL: test_tamper_shape (qdcheck.tests.test_indicators.TestCrossCheck.test_tamper_shape)` |
| `props` | 37 | round2 | `0` → `-1` | 抓住 | 抓住：`FAIL: test_causality_counts_checked_lines (qdcheck.tests.test_indicators.TestProperties.te` |
| `props` | 49 | round2 | `checked += 1` → `pass` | 抓住 | 抓住：`FAIL: test_causality_counts_checked_lines (qdcheck.tests.test_indicators.TestProperties.te` |
| `props` | 71 | round1 | `worst = max(worst, res['max_tol_ratio'])` → `pass` | 抓住 | 抓住：`FAIL: test_check_against_reports_mismatch (qdcheck.tests.test_indicators.TestCrossCheck.te` |
| `props` | 128 | round1 | `1.0` → `1.5` | 抓住 | 抓住：`FAIL: test_constant_input_closed_forms (qdcheck.tests.test_indicators.TestProperties.test_` |
| `stream` | 25 | round1 | `0.15` → `0.22499999999999998` | 漏网 | **仍漏网 → 未判定**：（缺判定） |
| `stream` | 26 | round1 | `0.005` → `0.0075` | 抓住 | 抓住：`FAIL: test_day_end_and_liquidation (qdcheck.tests.test_stream.TestSimulatorPieces.test_day` |
| `stream` | 27 | round1 | `0.01` → `0.015` | 抓住 | 抓住：`FAIL: test_new_orders_scripted (qdcheck.tests.test_stream.TestSimulatorPieces.test_new_ord` |
| `stream` | 38 | round2 | `0.4` → `0.6000000000000001` | 抓住 | 抓住：`FAIL: test_profile_distribution (qdcheck.tests.test_stream.TestSimulatorPieces.test_profil` |
| `stream` | 40 | round1 | `0.002` → `0.003` | 抓住 | 抓住：`FAIL: test_profile_distribution (qdcheck.tests.test_stream.TestSimulatorPieces.test_profil` |
| `stream` | 71 | round1 | `order.side < 0` → `order.side <= 0` | 漏网 | **仍漏网 → 未判定**：（缺判定） |
| `stream` | 79 | round2 | `Q > 0` → `Q >= 0` | 漏网 | **仍漏网 → 未判定**：（缺判定） |
| `stream` | 79 | round3_merged | `side > 0` → `side >= 0` | 漏网 | **仍漏网 → 未判定**：（缺判定） |
| `stream` | 83 | round1 | `mode == 'isolated'` → `mode != 'isolated'` | 抓住 | 抓住：`FAIL: test_affordability (qdcheck.tests.test_stream.TestSimulatorPieces.test_affordability` |
| `stream` | 88 | round1 | `3.0` → `4.5` | 抓住 | 抓住：`FAIL: test_affordability (qdcheck.tests.test_stream.TestSimulatorPieces.test_affordability` |
| `stream` | 92 | round1 | `q - inc` → `q + inc` | 抓住 | 抓住：`FAIL: test_affordability (qdcheck.tests.test_stream.TestSimulatorPieces.test_affordability` |
| `stream` | 130 | round1 | `Q != 0` → `Q == 0` | 抓住 | 抓住：`FAIL: test_day_end_and_liquidation (qdcheck.tests.test_stream.TestSimulatorPieces.test_day` |
| `stream` | 136 | round1 | `2.0 * self.rnd()` → `2.0 / self.rnd()` | 抓住 | 抓住：`FAIL: test_day_end_and_liquidation (qdcheck.tests.test_stream.TestSimulatorPieces.test_day` |
| `stream` | 148 | round1 | `1` → `0` | 抓住 | 抓住：`FAIL: test_check_stream_reports_tampering (qdcheck.tests.test_stream.TestScenariosAndCli.t` |
| `stream` | 148 | round1 | `1` → `2` | 抓住 | 抓住：`FAIL: test_day_end_and_liquidation (qdcheck.tests.test_stream.TestSimulatorPieces.test_day` |
| `stream` | 156 | round1 | `if rnd() < sim['p_cancel']:
    self.orders.remove(order)
  ` → `if not rnd() < sim['p_cancel']:
    self` | 抓住 | 抓住：`FAIL: test_new_orders_scripted (qdcheck.tests.test_stream.TestSimulatorPieces.test_new_ord` |
| `stream` | 159 | round1 | `rnd() >= sim['p_order'] or self.bars['volume'][t] == 0` → `rnd() >= sim['p_order'] and self.bars['v` | 抓住 | 抓住：`FAIL: test_aggregation_exit_code_and_defaults (qdcheck.tests.test_stream.TestSimulatorPiec` |
| `stream` | 165 | round1 | `if Q == 0 or u < 0.45:
    side = (1 if rnd() < 0.5 else -1)` → `if not (Q == 0 or u < 0.45):
    side = ` | 抓住 | 抓住：`FAIL: test_close_one_lot_and_compare_rows_scales (qdcheck.tests.test_stream.TestSimulatorP` |
| `stream` | 180 | round1 | `t + 1` → `t - 1` | 抓住 | 抓住：`FAIL: test_new_orders_scripted (qdcheck.tests.test_stream.TestSimulatorPieces.test_new_ord` |
| `stream` | 198 | round1 | `3000` → `3001` | 漏网 | **仍漏网 → 未判定**：（缺判定） |
| `stream` | 230 | round1 | `0.0` → `1.0` | 漏网 | **仍漏网 → 未判定**：（缺判定） |
| `stream` | 236 | round1 | `100000.0` → `150000.0` | 抓住 | 抓住：`FAIL: test_regenerates_within_tolerance (qdcheck.tests.test_vectors.TestCommittedVectors.t` |
| `stream` | 238 | round1 | `3` → `4` | 抓住 | 抓住：`FAIL: test_regenerates_within_tolerance (qdcheck.tests.test_vectors.TestCommittedVectors.t` |
| `stream` | 241 | round1 | `101.0` → `151.5` | 抓住 | 抓住：`FAIL: test_regenerates_within_tolerance (qdcheck.tests.test_vectors.TestCommittedVectors.t` |
| `stream` | 241 | round1 | `97.0` → `145.5` | 抓住 | 抓住：`FAIL: test_regenerates_within_tolerance (qdcheck.tests.test_vectors.TestCommittedVectors.t` |
| `stream` | 242 | round1 | `8` → `9` | 抓住 | 抓住：`FAIL: test_regenerates_within_tolerance (qdcheck.tests.test_vectors.TestCommittedVectors.t` |
| `stream` | 245 | round1 | `99.0` → `148.5` | 抓住 | 抓住：`FAIL: test_regenerates_within_tolerance (qdcheck.tests.test_vectors.TestCommittedVectors.t` |
| `stream` | 246 | round1 | `-1.25` → `1.25` | 抓住 | 抓住：`FAIL: test_regenerates_within_tolerance (qdcheck.tests.test_vectors.TestCommittedVectors.t` |
| `stream` | 248 | round1 | `4` → `3` | 抓住 | 抓住：`FAIL: test_file_headers (qdcheck.tests.test_vectors.TestCommittedVectors.test_file_headers` |
| `stream` | 248 | round1 | `15` → `16` | 抓住 | 抓住：`FAIL: test_regenerates_within_tolerance (qdcheck.tests.test_vectors.TestCommittedVectors.t` |
| `stream` | 260 | round1 | `0.95` → `1.4249999999999998` | 抓住 | 抓住：`FAIL: test_file_headers (qdcheck.tests.test_vectors.TestCommittedVectors.test_file_headers` |
| `stream` | 283 | round2 | `1e-09` → `1.5000000000000002e-09` | 抓住 | 抓住：`FAIL: test_aggregation_exit_code_and_defaults (qdcheck.tests.test_stream.TestSimulatorPiec` |
| `stream` | 288 | round1 | `0` → `-1` | 抓住 | 抓住：`FAIL: test_run_many_and_cli (qdcheck.tests.test_stream.TestScenariosAndCli.test_run_many_a` |
| `stream` | 288 | round1 | `0` → `1` | 抓住 | 抓住：`FAIL: test_run_many_and_cli (qdcheck.tests.test_stream.TestScenariosAndCli.test_run_many_a` |
| `stream` | 290 | round3_merged | `1.0` → `1.5` | 抓住 | 抓住：`ERROR: test_close_one_lot_and_compare_rows_scales (qdcheck.tests.test_stream.TestSimulator` |
| `stream` | 294 | round3_merged | `f == 'qty' and isinstance(a, int) and isinstance(b, int)` → `f == 'qty' or isinstance(a, int) or isin` | 抓住 | 抓住：`FAIL: test_close_one_lot_and_compare_rows_scales (qdcheck.tests.test_stream.TestSimulatorP` |
| `stream` | 328 | round2 | `agg_stats[k] = agg_stats.get(k, 0) + v` → `pass` | 抓住 | 抓住：`FAIL: test_aggregation_exit_code_and_defaults (qdcheck.tests.test_stream.TestSimulatorPiec` |
| `stream` | 330 | round1 | `agg_counters[k] = agg_counters.get(k, 0) + v` → `pass` | 漏网 | **仍漏网 → 未判定**：（缺判定） |
| `stream` | 335 | round2 | `2` → `1` | 漏网 | **仍漏网 → 未判定**：（缺判定） |
| `synth` | 38 | round1 | `4` → `3` | 抓住 | 抓住：`FAIL: test_defaults_and_small_knobs (qdcheck.tests.test_synth.TestSynth.test_defaults_and_` |
| `synth` | 38 | round1 | `4` → `5` | 抓住 | 抓住：`FAIL: test_defaults_and_small_knobs (qdcheck.tests.test_synth.TestSynth.test_defaults_and_` |
| `synth` | 38 | round1 | `1` → `2` | 抓住 | 抓住：`FAIL: test_defaults_and_small_knobs (qdcheck.tests.test_synth.TestSynth.test_defaults_and_` |
| `synth` | 119 | round1 | `sign * tick` → `sign / tick` | 抓住 | 抓住：`FAIL: test_defaults_and_small_knobs (qdcheck.tests.test_synth.TestSynth.test_defaults_and_` |
| `synth` | 154 | round1 | `u_gap < spec['gap_prob']` → `u_gap <= spec['gap_prob']` | 漏网 | **仍漏网 → 未判定**：（缺判定） |
| `synth` | 161 | round1 | `0` → `-1` | 抓住 | 抓住：`FAIL: test_defaults_and_small_knobs (qdcheck.tests.test_synth.TestSynth.test_defaults_and_` |
| `synth` | 161 | round2 | `0` → `1` | 抓住 | 抓住：`FAIL: test_defaults_and_small_knobs (qdcheck.tests.test_synth.TestSynth.test_defaults_and_` |
| `synth` | 189 | round1 | `i + 1` → `i - 1` | 抓住 | 抓住：`FAIL: test_defaults_and_small_knobs (qdcheck.tests.test_synth.TestSynth.test_defaults_and_` |
| `indicators_ref` | 127 | round1 | `return n` → `return None` | 代码已不存在 | 该处代码在后续提交中删除 / 改写（round1:indicators_ref:79:return_none） |
| `props` | 43 | round1 | `seed * 1000` → `seed / 1000` | 代码已不存在 | 该处代码在后续提交中删除 / 改写（round1:props:1325:binop） |
| `fuzz` | 173 | round2 | `depth + 1` → `depth - 1` | 代码已不存在 | 该处代码在后续提交中删除 / 改写（round2:fuzz:3560:binop） |
| `stream` | 123 | round2 | `order.filled += 1` → `pass` | 代码已不存在 | 该处代码在后续提交中删除 / 改写（round2:stream:538:delete_stmt） |
