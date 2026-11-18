require "digest"
require "fileutils"
require "find"
require "json"
require "open3"
require "optparse"
require "securerandom"
require "time"
require "tmpdir"

class HarnessCLI
  VERSION = "0.2.1"
  MODES = %w[audit refactor feature build].freeze
  CRITERIA = %w[correctness human_readability maintainability performance failure_handling scope_and_constraints].freeze
  CACHES = %w[.harness .git node_modules vendor .bundle .cache .next .vite .parcel-cache coverage dist build tmp log __pycache__].freeze
  DEPENDENCIES = /\A(?:Gemfile(?:\.lock)?|package(?:-lock)?\.json|yarn\.lock|pnpm-lock\.yaml|bun\.lockb?|requirements[^\/]*\.txt|Pipfile(?:\.lock)?|pyproject\.toml|poetry\.lock|composer\.(?:json|lock)|Cargo\.(?:toml|lock)|go\.(?:mod|sum))\z/

  def initialize(argv)
    @argv = argv.dup
    @options = {}
  end

  def call
    raise "Ruby 3.1 or later is required" if Gem::Version.new(RUBY_VERSION) < Gem::Version.new("3.1")
    @command = @argv.shift || "help"
    parser = OptionParser.new do |options|
      options.on("--project PATH") { |value| @options[:project] = value }
      options.on("--action TEXT") { |value| @options[:action] = value }
      options.on("--scope TEXT") { |value| @options[:scope] = value }
      options.on("--phase PHASE") { |value| @options[:phase] = value }
      options.on("--file PATH") { |value| @options[:file] = value }
      options.on("--answer-channel CHANNEL", %w[cli form chat]) { |value| @options[:answer_channel] = value }
      options.on("--id ID") { |value| @options[:id] = value }
      options.on("--reason TEXT") { |value| @options[:reason] = value }
    end
    parser.parse!(@argv)
    if @command == "help"
      puts File.read(File.join(runtime_root, "config/help.txt"))
      return 0
    end
    @project = File.realpath(@options.fetch(:project) { raise "Supply --project PATH" })
    raise "Project must be a directory" unless File.directory?(@project)
    @home = File.join(@project, ".harness")
    return initialize_project if @command == "init"
    return discover if @command == "discover"
    raise "Run init for this project first" unless File.file?(File.join(@home, "project.json"))
    File.open(File.join(@home, "runtime.lock"), "a") do |lock|
      lock.flock(File::LOCK_EX)
      dispatch
    end
  rescue StandardError => error
    warn JSON.generate(error: error.message, verdict: "revise")
    1
  end

  private

  def runtime_root
    File.expand_path("..", __dir__)
  end

  def read(path)
    JSON.parse(File.read(path, encoding: "UTF-8"))
  end

  def matching_paths(root, pattern, flags = 0)
    # An absolute glob can traverse denied parent directories in the Windows sandbox.
    Dir.glob(pattern, flags, base: root).map { |relative| File.join(root, relative) }
  end

  def canonical(object)
    case object
    when Hash then object.keys.sort.to_h { |key| [key, canonical(object[key])] }
    when Array then object.map { |value| canonical(value) }
    else object
    end
  end

  def hash_object(object)
    Digest::SHA256.hexdigest(JSON.generate(canonical(object)))
  end

  def file_hash(path)
    Digest::SHA256.file(path).hexdigest
  end

  def output(value)
    puts JSON.pretty_generate(value)
  end

  def immutable(path, value = nil, raw: false, **attributes)
    value = attributes unless attributes.empty?
    FileUtils.mkdir_p(File.dirname(path))
    File.open(path, File::WRONLY | File::CREAT | File::EXCL) do |file|
      file.write(raw ? value : JSON.pretty_generate(value) + "\n")
    end
    if @task_dir
      relative = path.delete_prefix(@task_dir + File::SEPARATOR).tr("\\", "/")
      ledger = File.join(@task_dir, "events.jsonl")
      previous = File.file?(ledger) ? File.readlines(ledger).last : nil
      previous_hash = previous ? JSON.parse(previous).fetch("sha256") : nil
      entry = { "path" => relative, "file_sha256" => file_hash(path), "previous" => previous_hash }
      entry["sha256"] = hash_object(entry)
      File.open(ledger, "a") { |file| file.puts(JSON.generate(entry)) }
    end
    path
  end

  def integrity_issues
    issues = []
    previous = nil
    registered = []
    ledger = File.join(@task_dir, "events.jsonl")
    return ["Task artifact ledger is missing"] unless File.file?(ledger)
    File.foreach(ledger) do |line|
      entry = JSON.parse(line)
      expected = entry.fetch("sha256")
      payload = entry.reject { |key, _| key == "sha256" }
      issues << "Artifact ledger was changed" unless entry["previous"] == previous && hash_object(payload) == expected
      path = bounded(@task_dir, entry.fetch("path"))
      registered << path
      issues << "Artifact tamper or missing file: #{entry['path']}" unless File.file?(path) && file_hash(path) == entry["file_sha256"]
      previous = expected
    end
    actual = matching_paths(@task_dir, "**/*", File::FNM_DOTMATCH).select { |path| File.file?(path) && path != ledger }
    issues << "Task contains unregistered evidence artifacts" unless actual.sort == registered.uniq.sort
    issues
  rescue StandardError => error
    ["Artifact ledger invalid: #{error.message}"]
  end

  def bounded(root, relative, must_exist: true)
    raise "Path must be a nonempty string" unless relative.is_a?(String) && !relative.empty?
    candidate = File.expand_path(relative, root)
    resolved = must_exist ? File.realpath(candidate) : candidate
    normalized_root = File.realpath(root)
    unless resolved == normalized_root || resolved.start_with?(normalized_root + File::SEPARATOR)
      raise "Path escapes its allowed directory: #{relative}"
    end
    resolved
  end

  def initialize_project
    raise ".harness already exists; init refuses to overwrite" if File.exist?(@home)
    FileUtils.mkdir(@home)
    %w[bin lib config docs templates].each do |name|
      origin = File.join(runtime_root, name)
      FileUtils.cp_r(origin, @home) if File.directory?(origin)
    end
    FileUtils.cp(File.join(@home, "config/project.example.json"), File.join(@home, "project.json"))
    output("installed" => @home, "version" => VERSION,
           "adapter_snippet" => "Read .harness/docs/AGENT_ADAPTER.md. Use ruby .harness/bin/harness prompt --project . for the agreed task. Read the pinned existing coding harness before implementation.")
    0
  end

  def source_snapshot
    files = {}
    Find.find(@project) do |path|
      next if path == @project
      if File.directory?(path) && CACHES.include?(File.basename(path))
        Find.prune
      end
      relative = path.delete_prefix(@project + File::SEPARATOR).tr("\\", "/")
      if File.symlink?(path)
        files[relative] = Digest::SHA256.hexdigest("symlink:" + File.readlink(path))
        Find.prune if File.directory?(path)
      elsif File.file?(path)
        files[relative] = file_hash(path)
      end
    end
    files = files.sort.to_h
    protected = files.select do |name, _|
      name.match?(%r{(?:\A|/)db/(?:migrate/|schema\.|structure\.)}) || File.basename(name).match?(DEPENDENCIES)
    end
    tracked = []
    begin
      listed, status = Open3.capture2("git", "-C", @project, "ls-files", "-z", err: File::NULL)
      tracked = listed.split("\0") if status.success?
    rescue Errno::ENOENT
      # The file snapshot also supports projects without Git.
    end
    { "files" => files, "protected" => protected, "tracked_protected" => protected.select { |name, _| tracked.include?(name) },
      "snapshot_sha256" => hash_object(files) }
  end

  def discover
    snapshot = source_snapshot
    names = snapshot["files"].keys
    rails = names.grep(%r{(?:\A|/)config/application\.rb\z}).filter_map do |name|
      root = name.sub(%r{config/application\.rb\z}, "").delete_suffix("/")
      prefix = root.empty? ? "" : root + "/"
      root.empty? ? "." : root if names.include?(prefix + "bin/rails")
    end
    react = names.grep(/(?:\A|\/)package\.json\z/).filter_map do |name|
      manifest = read(File.join(@project, name))
      dependencies = manifest.fetch("dependencies", {}).merge(manifest.fetch("devDependencies", {}))
      if dependencies.key?("react")
        { "root" => File.dirname(name), "scripts" => manifest.fetch("scripts", {}) }
      end
    rescue JSON::ParserError
      nil
    end
    candidates = rails.map { |root| { "root" => root, "argv" => ["ruby", "bin/rails", "test"], "executed" => false } }
    output("rails_roots" => rails, "react_roots" => react, "command_candidates" => candidates,
           "note" => "Candidates were inspected only. Configure explicit checks before execution.")
    0
  end

  def dispatch
    return start_task if @command == "start"
    load_task
    case @command
    when "questions" then output(read(File.join(@home, "config/questions.json"))); 0
    when "answer" then answer
    when "status" then output(current_status); 0
    when "prompt" then prompt
    when "run" then run_check(@argv.shift, @options[:phase])
    when "benchmark" then benchmark
    when "review" then review
    when "verify", "report" then verify
    else raise "Unknown command: #{@command}"
    end
  end

  def start_task
    mode = @argv.shift
    raise "Choose audit, refactor, feature, or build" unless MODES.include?(mode)
    action = @options[:action].to_s.strip
    scope = @options[:scope].to_s.strip
    raise "Action and scope are required" if action.empty? || scope.empty?
    decisions = @options[:file] ? read(File.expand_path(@options[:file], @project)) : {}
    raise "Task answers must be a JSON object" unless decisions.is_a?(Hash)
    decisions = decisions.map do |question, entry|
      option = entry.is_a?(Hash) ? entry.fetch("option") : entry
      validate_answer(question, option, mode)
      source = entry.is_a?(Hash) ? decision_source(entry.fetch("source"), question, option, action, scope) : direct_answer_source
      [question, option, source]
    end
    id = unique_id
    task_root = File.join(@home, "tasks")
    FileUtils.mkdir_p(task_root)
    target = bounded(task_root, id, must_exist: false)
    snapshot = source_snapshot
    config = project_config
    task_record = { "id" => id, "mode" => mode, "action" => action, "scope" => scope,
              "created_at" => Time.now.utc.iso8601, "initial_snapshot" => snapshot,
              "initial_config_sha256" => hash_object(config),
              "harness_version" => VERSION, "engine" => config["engine"], "agent" => config["agent"],
              "execution" => config.fetch("execution", { "max_repair_rounds" => 3 }),
              "questions_sha256" => file_hash(File.join(@home, "config/questions.json")) }
    Dir.mktmpdir("start-", @home) do |stage|
      @task_dir = File.join(stage, "task")
      immutable(File.join(@task_dir, "task.json"), task_record)
      record_snapshot(snapshot)
      decisions.each { |question, option, source| record_answer(question, option, source) }
      pointer = File.join(stage, "pointer")
      File.write(pointer, id)
      File.rename(@task_dir, target)
      begin
        File.rename(pointer, File.join(@home, "active_task"))
      rescue SystemCallError
        File.rename(target, @task_dir)
        raise
      end
    end
    @task_dir = target
    output("task_id" => id, "mode" => mode, "snapshot_sha256" => snapshot["snapshot_sha256"])
    0
  end

  def load_task
    id = File.read(File.join(@home, "active_task")).strip
    raise "Invalid task ID" unless id.match?(/\A[\w-]+\z/)
    @task_dir = bounded(File.join(@home, "tasks"), id)
    @task = read(File.join(@task_dir, "task.json"))
  rescue Errno::ENOENT
    raise "Start a task first"
  end

  def unique_id
    Time.now.utc.strftime("%Y%m%dT%H%M%S%6N") + "-" + SecureRandom.hex(4)
  end

  def record_snapshot(snapshot)
    digest = snapshot.fetch("snapshot_sha256")
    # Keep Windows paths short. The record retains and checks the full digest.
    [digest[0, 20], digest].each do |name|
      path = File.join(@task_dir, "snapshots", name + ".json")
      unless File.exist?(path)
        immutable(path, snapshot)
        return
      end
      return if read(path)["snapshot_sha256"] == digest
    end
    raise "Snapshot identity conflicts with existing evidence"
  end

  def project_config
    config = read(File.join(@home, "project.json"))
    raise "Project config must be an object" unless config.is_a?(Hash)
    config
  end

  def answer_records
    matching_paths(@task_dir, "answers/*.json").sort.each_with_object({}) do |path, result|
      data = read(path)
      result[data.fetch("question")] = data
    end
  end

  def answers
    answer_records.transform_values { |record| record.fetch("option") }
  end

  def direct_answer_source
    { "kind" => "task_answer", "channel" => @options.fetch(:answer_channel, "cli") }
  end

  def decision_source(input, question, option, action, scope)
    raise "A cited decision must include a source" unless input.is_a?(Hash)
    unless %w[code_changes migrations dependencies].include?(question) && option == "deny"
      raise "Cited restrictions can only deny a change. Record other choices explicitly for this task."
    end
    quote = input.fetch("quote").to_s.strip
    raise "Include the exact text of the decision" if quote.empty?
    source = { "kind" => input["kind"], "quote" => quote, "channel" => @options.fetch(:answer_channel, "cli") }
    case input["kind"]
    when "task_request"
      raise "The cited text is absent from this task's action and scope" unless [action, scope].any? { |text| text.include?(quote) }
    when "project_file"
      path = bounded(@project, input.fetch("path"))
      relative = path.delete_prefix(@project + File::SEPARATOR).tr("\\", "/")
      if relative.split("/").any? { |part| CACHES.include?(part) } || !%w[.md .txt].include?(File.extname(relative).downcase)
        raise "Choose a project Markdown or text decision file outside runtime and cache directories"
      end
      raise "Decision files must be at most 1 MiB" if File.size(path) > 1_048_576
      content = File.binread(path).force_encoding("UTF-8")
      unless content.valid_encoding? && content.gsub("\r\n", "\n").include?(quote.gsub("\r\n", "\n"))
        raise "The cited text is absent from the project decision file"
      end
      source.merge!("path" => relative, "sha256" => Digest::SHA256.hexdigest(content))
    else
      raise "Choose task_request or project_file as the source"
    end
    source
  end

  def validate_answer(id, option, mode)
    question = read(File.join(@home, "config/questions.json")).fetch("questions").find { |row| row["id"] == id }
    raise "Unknown question" unless question
    unless option.is_a?(String) && !option.strip.empty? && (question["custom"] || question.fetch("options").include?(option))
      raise "Choose an explicit allowed answer for #{id}"
    end
    raise "Audit mode requires read-only application access" if id == "code_changes" && mode == "audit" && option != "deny"
  end

  def record_answer(id, option, source = direct_answer_source)
    immutable(File.join(@task_dir, "answers", unique_id + ".json"),
              "question" => id, "option" => option, "source" => source, "recorded_at" => Time.now.utc.iso8601)
  end

  def answer
    id, option = @argv
    validate_answer(id, option, @task["mode"])
    source = if @options[:file]
      decision_source(read(File.expand_path(@options[:file], @project)), id, option, @task["action"], @task["scope"])
    else
      direct_answer_source
    end
    record_answer(id, option, source)
    output("question" => id, "option" => option, "source" => source)
    0
  end

  def engine_issues
    engine = project_config.fetch("engine", {})
    path = engine["instructions_path"]
    expected = engine["sha256"]
    return ["Configure the existing coding harness engine.instructions_path and sha256"] unless path.is_a?(String) && !path.empty? && expected.to_s.match?(/\A[0-9a-f]{64}\z/i)
    resolved = File.expand_path(path, @project)
    return ["Coding harness instructions are missing"] unless File.file?(resolved)
    return ["Coding harness instruction hash changed"] unless file_hash(resolved) == expected.downcase
    []
  end

  def agreement_issues
    issues = integrity_issues + engine_issues
    if file_hash(File.join(@home, "config/questions.json")) != @task["questions_sha256"]
      issues << "Required question definitions changed after task start"
    end
    current = answers
    read(File.join(@home, "config/questions.json")).fetch("questions").each do |question|
      issues << "Required question unresolved: #{question['id']}" if question["required"] && !current.key?(question["id"])
    end
    issues << "Action or scope is missing" if @task["action"].to_s.empty? || @task["scope"].to_s.empty?
    issues
  end

  def require_agreement
    issues = agreement_issues
    raise issues.join("; ") unless issues.empty?
  end

  def pin_execution_config
    path = File.join(@task_dir, "execution_config.json")
    current = project_config
    if File.file?(path)
      raise "Execution configuration changed after verification started" unless read(path) == current
    else
      immutable(path, current)
    end
  end

  def prompt
    require_agreement
    engine = project_config.fetch("engine")
    guide = File.join(@home, "docs/WORKFLOW.md")
    puts "ACTION\n#{@task['action']}\nScope: #{@task['scope']}\nMode: #{@task['mode']}\n\n"
    puts "CONSTRAINTS\nExplicit answers: #{JSON.generate(answers)}\nRead the configured coding harness at #{File.expand_path(engine['instructions_path'], @project)}.\nPinned SHA-256: #{engine['sha256']}\nInstalled orchestration path: #{@home}\n\n"
    puts "VERIFICATION\nRun the configured baseline checks before application changes. Use only the agreed scope. Record actual results and resolve failed checks before completion.\n\n"
    puts "REPORTING\nRecord agent-authored review evidence tied to the current snapshot. Run verify and report all failed or missing gates.\n\n"
    puts File.read(guide) if File.file?(guide)
    0
  end

  def checks
    configured = project_config.fetch("checks", [])
    raise "Checks must be an array" unless configured.is_a?(Array)
    names = configured.map { |row| row.fetch("name") }
    raise "Check names must be unique" unless names.uniq == names
    configured.each do |row|
      raise "Invalid check name" unless row["name"].is_a?(String) && row["name"].match?(/\A[a-z][a-z0-9_-]*\z/)
      argv = row["argv"]
      raise "Checks require explicit argv arrays" unless argv.is_a?(Array) && !argv.empty? && argv.all? { |item| item.is_a?(String) && !item.empty? && !item.include?("\0") }
      raise "Required and benchmark flags must be boolean" unless [true, false].include?(row["required"]) && [true, false].include?(row["benchmark"])
      phases = row.fetch("phases", %w[baseline after])
      raise "A check must run baseline and after, or after only" unless [%w[baseline after], %w[after]].include?(phases)
      raise "Benchmarks require both baseline and after" if row["benchmark"] && phases != %w[baseline after]
      raise "Check cwd must be a directory inside the project" unless File.directory?(bounded(@project, row.fetch("cwd", ".")))
    end
    configured
  end

  def benchmark_contract
    config = project_config.fetch("benchmark")
    %w[name version workload].each do |key|
      raise "Benchmark #{key} is required" unless config[key].is_a?(String) && !config[key].strip.empty?
    end
    %w[cases metrics assertions checks].each do |key|
      raise "Benchmark #{key} must be a nonempty array" unless config[key].is_a?(Array) && !config[key].empty?
    end
    selected = checks.select { |check| config["checks"].include?(check["name"]) }
    raise "Benchmark checks must exist and have benchmark=true" unless selected.length == config["checks"].uniq.length && selected.all? { |check| check["benchmark"] }
    scripts = config.fetch("scripts", [])
    raise "Benchmark scripts must be an array" unless scripts.is_a?(Array)
    scripts = scripts.map do |name|
      path = bounded(@project, name)
      raise "Benchmark script must be a file" unless File.file?(path)
      [path.delete_prefix(@project + File::SEPARATOR).tr("\\", "/"), file_hash(path)]
    end.to_h
    { "contract" => config, "checks" => selected, "scripts" => scripts }
  end

  def baseline_source_issues(current, contract = nil)
    initial = @task.fetch("initial_snapshot").fetch("files")
    files = current.fetch("files").dup
    if contract
      contract.fetch("scripts").each_key { |path| files.delete(path.tr("\\", "/")) unless initial.key?(path.tr("\\", "/")) }
    end
    files == initial ? [] : ["Application source changed before baseline verification"]
  end

  def results(name, phase)
    matching_paths(@task_dir, File.join("results", name, phase, "*/result.json")).sort.map { |path| read(path) }
  end

  def run_check(name, phase, contract: nil)
    require_agreement
    raise "Phase must be baseline or after" unless %w[baseline after].include?(phase)
    check = checks.find { |row| row["name"] == name }
    raise "Unknown check: #{name}" unless check
    phases = check.fetch("phases", %w[baseline after])
    raise "After-only checks require feature or build mode" if phases == %w[after] && !%w[feature build].include?(@task["mode"])
    raise "This check does not define the requested phase" unless phases.include?(phase)
    if check["benchmark"] && contract.nil?
      raise "Run benchmark through the benchmark command so its contract is frozen"
    end
    before = source_snapshot
    if phase == "baseline"
      declared_contract = contract
      if declared_contract.nil? && project_config.fetch("benchmark", {}).fetch("scripts", []).any?
        declared_contract = benchmark_contract
      end
      problems = baseline_source_issues(before, declared_contract)
      raise problems.join("; ") unless problems.empty?
    elsif phases.include?("baseline")
      baseline = results(name, "baseline").last
      raise "A passing baseline is required for #{name}" unless baseline && baseline["exit_code"] == 0 && !baseline["source_changed"]
      raise "Check configuration changed after baseline" unless baseline["check_sha256"] == hash_object(check)
    end
    pin_execution_config
    run = File.join(@task_dir, "results", name, phase, unique_id)
    FileUtils.mkdir_p(run)
    started_at = Time.now.utc.iso8601
    started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
    stdout = ""
    stderr = ""
    exit_code = nil
    spawn_error = nil
    begin
      argv = check.fetch("argv")
      stdout, stderr, status = Open3.capture3([argv.first, argv.first], *argv.drop(1), chdir: bounded(@project, check.fetch("cwd", ".")))
      exit_code = status.exitstatus || 128
    rescue SystemCallError => error
      spawn_error = error.message
      stderr = error.message
      exit_code = 127
    end
    elapsed = Process.clock_gettime(Process::CLOCK_MONOTONIC) - started
    after = source_snapshot
    immutable(File.join(run, "stdout.log"), stdout, raw: true)
    immutable(File.join(run, "stderr.log"), stderr, raw: true)
    result = { "check" => name, "phase" => phase, "argv" => check["argv"], "cwd" => check.fetch("cwd", "."),
               "check_sha256" => hash_object(check), "source_before_sha256" => before["snapshot_sha256"],
               "source_after_sha256" => after["snapshot_sha256"], "source_changed" => before["files"] != after["files"],
               "exit_code" => exit_code, "spawn_error" => spawn_error, "started_at" => started_at,
               "duration_seconds" => elapsed.round(6), "stdout_sha256" => file_hash(File.join(run, "stdout.log")),
               "stderr_sha256" => file_hash(File.join(run, "stderr.log")),
               "benchmark_contract_sha256" => contract ? hash_object(contract) : nil }
    immutable(File.join(run, "result.json"), result)
    record_snapshot(after)
    output(result)
    exit_code.zero? && !result["source_changed"] ? 0 : 1
  end

  def benchmark
    require_agreement
    phase = @options[:phase]
    raise "Phase must be baseline or after" unless %w[baseline after].include?(phase)
    current = benchmark_contract
    path = File.join(@task_dir, "benchmark/contract.json")
    if File.file?(path)
      raise "Benchmark contract or script changed after it was frozen" unless read(path) == current
    elsif phase == "baseline"
      issues = baseline_source_issues(source_snapshot, current)
      raise issues.join("; ") unless issues.empty?
      immutable(path, current)
    else
      raise "Run a passing benchmark baseline before the after phase"
    end
    statuses = current.fetch("checks").map { |check| run_check(check["name"], phase, contract: current) }
    statuses.all?(&:zero?) ? 0 : 1
  end

  def review
    require_agreement
    file = @options.fetch(:file) { raise "Supply --file PATH" }
    candidate = read(File.expand_path(file, @project))
    snapshot = source_snapshot
    issues = review_issues(candidate, snapshot["snapshot_sha256"])
    immutable(File.join(@task_dir, "reviews", unique_id + ".json"), candidate.merge("review_type" => "agent_authored", "validation_issues" => issues))
    output("review_type" => "agent_authored", "verdict" => issues.empty? ? "pass" : "revise", "issues" => issues)
    issues.empty? ? 0 : 1
  end

  def review_issues(data, snapshot_hash)
    return ["Review must be a structured object"] unless data.is_a?(Hash)
    issues = []
    issues << "Review is for a different source snapshot" unless data["snapshot_sha256"] == snapshot_hash
    issues << "Review must identify author as agent" unless data["author"] == "agent"
    issues << "Review verdict is not pass" unless data["verdict"] == "pass"
    criteria = data["criteria"]
    return issues + ["Review must record all quality criteria"] unless criteria.is_a?(Array) && criteria.all? { |row| row.is_a?(Hash) }
    issues << "Review criterion IDs must be unique" unless criteria.map { |row| row["id"] }.uniq.length == criteria.length
    CRITERIA.each do |id|
      row = criteria.find { |item| item["id"] == id }
      unless row && row["evidence"].is_a?(Array) && row["evidence"].any? { |evidence| evidence.is_a?(String) && !evidence.strip.empty? }
        issues << "Semantic review unverified: #{id} has no evidence"
        next
      end
      allowed = row["verdict"] == "pass" || row["verdict"] == "not_applicable" && row["reason"].is_a?(String) && !row["reason"].strip.empty?
      issues << "Semantic review unverified or revise: #{id}" unless allowed
    end
    issues
  end

  def completion_issues(snapshot)
    issues = agreement_issues
    config_pin = File.join(@task_dir, "execution_config.json")
    if File.file?(config_pin) && read(config_pin) != project_config
      issues << "Execution configuration changed after verification started"
    end
    allowed = answers
    initial = @task.fetch("initial_snapshot")
    if @task["mode"] == "audit" && initial["files"] != snapshot["files"]
      issues << "Audit must not change application source"
    elsif allowed["code_changes"] == "deny" && initial["files"] != snapshot["files"]
      issues << "Application source changed without code-change permission"
    end
    if MODES.include?(@task["mode"])
      old = initial.fetch("protected")
      new_files = snapshot.fetch("protected")
      changed = (old.keys | new_files.keys).select { |path| old[path] != new_files[path] }
      changed.each do |path|
        kind = File.basename(path).match?(DEPENDENCIES) ? "dependencies" : "migrations"
        issues << "Forbidden #{kind} change: #{path}" unless allowed[kind] == "allow"
      end
    end
    configured = checks
    required = configured.select { |check| check["required"] || check["benchmark"] }
    issues << "Configure at least one required behavior or verification check" if required.empty?
    required.each do |check|
      phases = check.fetch("phases", %w[baseline after])
      issues << "After-only checks require feature or build mode: #{check['name']}" if phases == %w[after] && !%w[feature build].include?(@task["mode"])
      phases.each do |phase|
        result = results(check["name"], phase).last
        if result.nil?
          issues << "Required #{phase} check has not run: #{check['name']}"
          next
        end
        issues << "Required #{phase} check failed: #{check['name']}" unless result["exit_code"] == 0 && !result["source_changed"]
        issues << "Check configuration changed: #{check['name']}" unless result["check_sha256"] == hash_object(check)
        if phase == "after" && result["source_after_sha256"] != snapshot["snapshot_sha256"]
          issues << "After evidence is stale after source mutation: #{check['name']}"
        end
      end
    end
    benchmark_checks = configured.select { |check| check["benchmark"] }
    unless benchmark_checks.empty?
      contract_path = File.join(@task_dir, "benchmark/contract.json")
      if !File.file?(contract_path)
        issues << "Required benchmark contract and baseline are missing"
      elsif read(contract_path) != benchmark_contract
        issues << "Benchmark contract or scripts drifted after baseline"
      end
    end
    last_review = matching_paths(@task_dir, "reviews/*.json").sort.last
    issues.concat(last_review ? review_issues(read(last_review), snapshot["snapshot_sha256"]) : ["Agent-authored semantic review is unverified"])
    issues.uniq
  end

  def current_status
    snapshot = source_snapshot
    issues = completion_issues(snapshot)
    { "task_id" => @task["id"], "mode" => @task["mode"], "action" => @task["action"], "scope" => @task["scope"],
      "snapshot_sha256" => snapshot["snapshot_sha256"], "verdict" => issues.empty? ? "pass" : "revise", "issues" => issues,
      "semantic_review" => "Agent-authored evidence is reported separately from executable checks. No independent review is claimed." }
  end

  def verify
    report = current_status
    immutable(File.join(@task_dir, "reports", unique_id + ".json"), report)
    output(report)
    report["verdict"] == "pass" ? 0 : 1
  end
end
