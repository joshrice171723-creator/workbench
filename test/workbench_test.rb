require "minitest/autorun"
require "tmpdir"
require "fileutils"
require "stringio"
require "rbconfig"
require_relative "../lib/harness"
require_relative "../lib/workbench"

class WorkbenchTest < Minitest::Test
  def setup
    @project = Dir.mktmpdir("workbench-test-")
    File.write(File.join(@project, "AGENTS.md"), "Keep this rule.\n")
    File.write(File.join(@project, "app.rb"), "VALUE = 1\n")
  end

  def teardown
    FileUtils.remove_entry(@project)
  end

  def call(*args)
    out, err = StringIO.new, StringIO.new
    oldout, olderr = $stdout, $stderr
    $stdout, $stderr = out, err
    code = HarnessCLI.new(args + ["--project", @project]).call
    [code, out.string + err.string]
  ensure
    $stdout, $stderr = oldout, olderr
  end

  def ok(*args)
    code, text = call(*args)
    assert_equal 0, code, text
    text
  end

  def bad(*args)
    code, text = call(*args)
    refute_equal 0, code, text
    text
  end

  def installed
    JSON.parse(ok("setup", "init"))
  end

  def config
    File.join(@project, ".harness/project.json")
  end

  def started(mode = "refactor")
    JSON.parse(ok(mode, "--action", "Improve this scoped operation", "--scope", "app.rb"))
  end

  def answers
    { "scope_confirmed" => "confirmed", "code_changes" => "allow_scoped", "migrations" => "deny",
      "dependencies" => "deny", "engine_integration" => "confirmed" }.each { |id, value| ok("internal", "answer", id, value) }
  end

  def test_install_pins_generic_engine_and_preserves_existing_rules
    result = installed
    assert_equal "0.2.1", result["version"]
    assert_equal "Keep this rule.\n", File.read(File.join(@project, "AGENTS.md"))
    settings = JSON.parse(File.read(config))
    assert_equal "gpt-6.2-astra", settings.dig("agent", "model")
    refute_empty settings.dig("engine", "included_files")
    refute Dir.exist?(File.join(@project, ".harness/product-spec"))
    bad("setup", "init")
  end

  def test_all_mode_aliases_use_the_same_task_records
    installed
    %w[audit refactor feature build].each do |mode|
      task = started(mode)
      detail = JSON.parse(ok("runs", "show", task["task_id"]))
      assert_equal mode, detail["task"]["mode"]
      assert_equal "0.2.1", detail["task"]["harness_version"]
      assert_equal "gpt-6.2-astra", detail.dig("task", "agent", "model")
      ok("task", "stop", "--reason", "User ended this test task")
    end
  end

  def test_active_task_cannot_be_replaced
    installed
    first = started
    assert_includes bad("feature", "--action", "Another action", "--scope", "app.rb"), "active task"
    assert_equal first["task_id"], File.read(File.join(@project, ".harness/active_task"))
  end

  def test_unreadable_lifecycle_preserves_the_active_pointer_and_evidence
    installed
    id = started.fetch("task_id")
    folder = File.join(@project, ".harness/tasks", id)
    events = File.binread(File.join(folder, "events.jsonl"))
    Dir.mkdir(File.join(folder, "lifecycle"))
    File.write(File.join(folder, "lifecycle/latest.json"), "incomplete")
    status = JSON.parse(ok("task", "status"))
    assert_equal "unverified", status.dig("lifecycle", "status")
    assert_equal "unverified", status["verdict"]
    assert_includes status["issues"].first, "Cannot read task lifecycle"
    assert_equal "unverified", JSON.parse(ok("runs", "list")).fetch("runs").first.dig("lifecycle", "status")
    assert_includes bad("task", "finish"), "Cannot read task lifecycle"
    assert_includes bad("audit", "--action", "Read", "--scope", "app.rb"), "Cannot read task lifecycle"
    assert_equal "unverified", JSON.parse(bad("task", "verify"))["verdict"]
    assert_equal id, File.read(File.join(@project, ".harness/active_task")).strip
    assert_equal events, File.binread(File.join(folder, "events.jsonl"))
  end

  def test_pause_resume_and_history_preserve_tasks
    installed
    first = started
    ok("task", "pause", "--reason", "User paused this work")
    second = started("audit")
    assert_includes bad("task", "resume", "--id", first["task_id"]), "Another active task"
    ok("task", "stop", "--reason", "User stopped second task")
    ok("task", "resume", "--id", first["task_id"])
    result = JSON.parse(ok("runs", "compare", first["task_id"], second["task_id"]))
    assert_equal 2, result["runs"].length
    assert_equal "active", result["runs"][0].dig("lifecycle", "status")
    assert_equal "stopped", result["runs"][1].dig("lifecycle", "status")
  end

  def test_new_task_does_not_inherit_answers
    installed
    started
    answers
    ok("task", "stop", "--reason", "User ended first task")
    started
    questions = JSON.parse(ok("internal", "questions"))
    assert_empty questions["answers"]
    assert_equal 5, questions["missing_required"].length
    assert questions["questions"].first.key?("option_details")
    assert_equal false, questions["questions"].first["suggestion_is_permission"]
  end

  def test_engine_included_file_drift_blocks_prompt
    installed
    started
    answers
    File.write(File.join(@project, ".harness/engine/STANDARDS.md"), "Weakened rules")
    assert_includes bad("task", "prompt"), "included file changed"
  end

  def test_configuration_validation_restores_invalid_changes
    installed
    before = File.binread(config)
    proposed = JSON.parse(before)
    proposed["engine"]["sha256"] = "0" * 64
    input = File.join(@project, ".harness/proposed.json")
    File.write(input, JSON.generate(proposed))
    bad("setup", "configure", "--file", input)
    assert_equal before, File.binread(config)
  end

  def test_engine_identity_cannot_change_during_active_task
    installed
    started
    proposed = JSON.parse(File.read(config))
    proposed["agent"]["model"] = "different-model"
    input = File.join(@project, ".harness/proposed.json")
    File.write(input, JSON.generate(proposed))
    assert_includes bad("setup", "configure", "--file", input), "active task"
  end

  def test_checks_can_be_configured_before_first_baseline
    installed
    started
    settings = JSON.parse(File.read(config))
    settings["checks"] = [{ "name" => "behavior", "argv" => [RbConfig.ruby, "-e", "puts 'Actual check'"], "cwd" => ".", "required" => true, "benchmark" => false }]
    input = File.join(@project, ".harness/proposed.json")
    File.write(input, JSON.generate(settings))
    ok("setup", "configure", "--file", input)
    answers
    ok("internal", "run", "behavior", "--phase", "baseline")
    assert_includes bad("setup", "configure", "--file", input), "frozen"
  end

  def test_repair_limit_is_fixed_in_task_and_finish_requires_evidence
    installed
    started
    answers
    3.times { ok("task", "repair-round", "--reason", "Repair a confirmed finding") }
    assert_includes bad("task", "finish"), "cannot finish"
    assert_includes bad("task", "repair-round", "--reason", "One extra repair"), "limit reached"
    status = JSON.parse(ok("task", "status"))
    assert_equal 3, status["repair_rounds"]
    assert_equal "stopped", status.dig("lifecycle", "status")
    assert_includes bad("task", "resume"), "exhausted"
  end

  def test_current_checks_and_review_allow_explicit_finish
    installed
    settings = JSON.parse(File.read(config))
    settings["checks"] = [{ "name" => "behavior", "argv" => [RbConfig.ruby, "-e", "puts 'Actual check'"], "cwd" => ".", "required" => true, "benchmark" => false }]
    File.write(config, JSON.generate(settings))
    task = started
    answers
    ok("internal", "run", "behavior", "--phase", "baseline")
    ok("internal", "run", "behavior", "--phase", "after")
    snapshot = JSON.parse(ok("task", "status"))["snapshot_sha256"]
    review = { "schema_version" => 1, "snapshot_sha256" => snapshot, "author" => "agent", "verdict" => "pass",
               "criteria" => HarnessCLI::CRITERIA.map { |id| { "id" => id, "verdict" => "pass", "evidence" => ["Synthetic mechanism test only."] } } }
    path = File.join(@project, ".harness/test-review.json")
    File.write(path, JSON.generate(review))
    ok("internal", "review", "--file", path)
    ok("task", "finish")
    status = JSON.parse(ok("task", "status"))
    assert_equal "completed", status.dig("lifecycle", "status")
    assert_equal "pass", status["verdict"]
    assert_includes bad("internal", "run", "behavior", "--phase", "after"), "completed"
    assert_equal task["task_id"], JSON.parse(ok("runs", "list"))["runs"].first["id"]
  end

  def test_invalid_wizard_answers_leave_the_project_without_a_task
    installed
    path = File.join(@project, ".harness/choices.json")
    File.write(path, JSON.generate("code_changes" => "allow_scoped"))
    assert_includes bad("audit", "--action", "Inspect", "--scope", "app.rb", "--file", path), "read-only"
    assert_empty JSON.parse(ok("runs", "list")).fetch("runs")
    File.write(path, JSON.generate("code_changes" => "deny"))
    ok("audit", "--action", "Inspect", "--scope", "app.rb", "--file", path)
    assert_equal "deny", JSON.parse(ok("internal", "questions")).dig("answers", "code_changes")
  end

  def test_only_current_explicit_restrictions_skip_questions_and_retain_their_sources
    installed
    rules = File.join(@project, "DECISIONS.md")
    File.write(rules, "Leave dependencies unchanged.\n")
    input = File.join(@project, ".harness/decisions.json")
    values = {
      "scope_confirmed" => "confirmed",
      "migrations" => { "option" => "deny", "source" => { "kind" => "task_request", "quote" => "Keep schema fixed." } },
      "dependencies" => { "option" => "deny", "source" => { "kind" => "project_file", "path" => "DECISIONS.md", "quote" => "Leave dependencies unchanged." } }
    }
    File.write(input, JSON.generate(values))
    ok("refactor", "--action", "Simplify the method", "--scope", "app.rb. Keep schema fixed.", "--file", input, "--answer-channel", "form")
    current = JSON.parse(ok("internal", "questions"))
    assert_equal %w[code_changes engine_integration], current["missing_required"]
    assert_equal "allow_scoped", current["unanswered_questions"].first["suggested_answer"]
    assert_nil current.dig("answers", "code_changes")
    sources = current["questions"].to_h { |question| [question["id"], question["answer_source"]] }
    assert_equal Digest::SHA256.file(rules).hexdigest, sources.dig("dependencies", "sha256")
    assert_equal "Keep schema fixed.", sources.dig("migrations", "quote")
    assert_equal "form", sources.dig("scope_confirmed", "channel")
    ok("task", "stop", "--reason", "End the first agreement")
    started("audit")
    next_task = JSON.parse(ok("internal", "questions"))
    assert_equal 5, next_task["missing_required"].size
    assert_empty next_task["answers"]
  end

  def test_unsubstantiated_or_permissive_citations_never_create_a_task
    installed
    input = File.join(@project, ".harness/proposal.json")
    decision = { "kind" => "project_file", "path" => "AGENTS.md", "quote" => "This text does not exist" }
    values = { "dependencies" => { "option" => "deny", "source" => decision } }
    File.write(input, JSON.generate(values))
    call_args = ["feature", "--action", "Add feature", "--scope", "app.rb", "--file", input]
    assert_includes bad(*call_args), "absent"
    decision["quote"] = "Keep this rule."
    values["dependencies"]["option"] = "allow"
    File.write(input, JSON.generate(values))
    assert_includes bad(*call_args), "only deny"
    assert_empty JSON.parse(ok("runs", "list"))["runs"]
  end

  def test_inspecting_an_engine_hashes_only_declared_project_files_and_does_not_install_it
    installed
    before = File.binread(config)
    input = File.join(@project, ".harness/engine-proposal.json")
    File.write(input, JSON.generate("version" => "local-1", "instructions_path" => "AGENTS.md", "files" => []))
    result = JSON.parse(ok("setup", "inspect-engine", "--file", input))
    assert_equal Digest::SHA256.file(File.join(@project, "AGENTS.md")).hexdigest, result["sha256"]
    assert_equal ["AGENTS.md"], result["included_files"].keys
    assert_equal before, File.binread(config)
    assert JSON.parse(ok("setup", "check-engine"))["verified"]
  end

  def test_incomplete_task_creation_leaves_no_visible_task
    installed
    command = HarnessCLI.new(["audit", "--action", "Inspect", "--scope", "app.rb", "--project", @project])
    def command.record_snapshot(_snapshot)
      raise IOError, "Simulated write failure"
    end
    _, errors = capture_io { assert_equal 1, command.call }
    assert_includes errors, "Simulated write failure"
    assert_empty JSON.parse(ok("runs", "list"))["runs"]
    refute File.exist?(File.join(@project, ".harness/active_task"))
  end

  def test_task_snapshot_remains_readable_in_a_nested_workspace
    outer = @project
    component_length = [136 - outer.size - 1, 1].max
    @project = File.join(outer, "p" * component_length)
    FileUtils.mkdir_p(@project)
    installed
    id = started["task_id"]
    snapshot = Dir.glob(File.join(@project, ".harness/tasks", id, "snapshots/*.json")).fetch(0)
    assert_operator snapshot.size, :<, 260 if Gem.win_platform?
    assert_match(/\A[0-9a-f]{64}\z/, JSON.parse(File.read(snapshot))["snapshot_sha256"])
    assert_equal "pass", JSON.parse(ok("runs", "show", id)).dig("ledger", "verdict")
  ensure
    @project = outer
  end

  def test_feature_can_verify_new_behavior_after_implementation
    installed
    settings = JSON.parse(File.read(config))
    settings["checks"] = [{ "name" => "new_feature", "argv" => [RbConfig.ruby, "-e", "load 'app.rb'; abort unless VALUE == 2"],
                            "cwd" => ".", "required" => true, "benchmark" => false, "phases" => ["after"] }]
    File.write(config, JSON.generate(settings))
    started("feature")
    answers
    assert_includes bad("internal", "run", "new_feature", "--phase", "baseline"), "does not define"
    File.write(File.join(@project, "app.rb"), "VALUE = 2\n")
    assert_equal 0, JSON.parse(ok("internal", "run", "new_feature", "--phase", "after"))["exit_code"]
    issues = JSON.parse(ok("task", "status")).fetch("issues")
    assert_equal ["Agent-authored semantic review is unverified"], issues
    assert_includes bad("internal", "answer", "dependencies", "allow"), "frozen"
    ok("task", "stop", "--reason", "End feature check")
    started("refactor")
    answers
    assert_includes bad("internal", "run", "new_feature", "--phase", "after"), "feature or build"
  end

  def test_benchmark_wraps_each_measured_phase_in_a_single_record
    installed
    settings = JSON.parse(File.read(config))
    File.write(File.join(@project, "verify.rb"), "load 'app.rb'; abort unless VALUE == 1; puts VALUE\n")
    settings["checks"] = [{ "name" => "measure", "argv" => [RbConfig.ruby, "verify.rb"], "cwd" => ".", "required" => true, "benchmark" => true }]
    settings["benchmark"] = { "name" => "Value lookup", "version" => "1", "workload" => "Read one value",
      "cases" => ["initial"], "metrics" => ["value"], "assertions" => ["value equals one"], "checks" => ["measure"], "scripts" => ["verify.rb"] }
    File.write(config, JSON.generate(settings))
    started
    answers
    receipts = %w[baseline after].map do |phase|
      result = JSON.parse(ok("internal", "benchmark", "--phase", phase))
      assert_equal "pass", result["verdict"]
      assert_equal phase, result.fetch("checks").first.fetch("phase")
      result.fetch("checks").first
    end
    assert_equal receipts.first["benchmark_contract_sha256"], receipts.last["benchmark_contract_sha256"]
    detail = JSON.parse(ok("runs", "show", JSON.parse(ok("task", "status"))["task_id"]))
    assert_equal "Value lookup", detail.dig("benchmark", "contract", "name")
    assert_equal "1", detail["checks"].first["stdout_preview"].strip
    assert_equal "pass", detail.dig("ledger", "verdict")
  end
end
