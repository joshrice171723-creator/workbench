module WorkbenchCommands
  def call
    case @argv.first
    when "setup"
      @argv.shift
      allowed = %w[init discover configure inspect-engine check-engine]
      @argv[0] = "unknown-setup-command" unless allowed.include?(@argv.first)
    when "task"
      @argv.shift
      @argv[0] = "task-" + @argv.first.to_s unless %w[status report prompt verify].include?(@argv.first)
    when "runs"
      @argv.shift
      @argv[0] = "runs-" + (@argv.first || "list")
    when "internal"
      @argv.shift
    when *HarnessCLI::MODES
      @argv.unshift("start")
    end
    super
  end

  private

  def initialize_project
    raise ".harness already exists; init refuses to overwrite" if File.exist?(@home)
    FileUtils.mkdir(@home)
    %w[lib config docs templates engine web].each do |name|
      origin = File.join(runtime_root, name)
      FileUtils.cp_r(origin, @home) if File.directory?(origin)
    end
    FileUtils.mkdir(File.join(@home, "bin"))
    %w[bin/harness bin/agent_bridge.py agent_adapter.py chat_workflow.py server.py].each do |name|
      FileUtils.cp(File.join(runtime_root, name), File.join(@home, name))
    end
    config = read(File.join(@home, "config/project.example.json"))
    instruction = ".harness/engine/ENGINE.md"
    included = matching_paths(@home, "engine/**/*").select { |path| File.file?(path) }.sort.to_h do |path|
      [path.delete_prefix(@project + File::SEPARATOR).tr("\\", "/"), file_hash(path)]
    end
    config["engine"] = { "version" => "generic-2.10.6.3.1-derived-0.2.0", "instructions_path" => instruction,
                         "sha256" => file_hash(File.join(@project, instruction)), "included_files" => included,
                         "included_files_sha256" => hash_object(included) }
    config["execution"] = { "max_repair_rounds" => 3 }
    File.write(File.join(@home, "project.json"), JSON.pretty_generate(config) + "\n")
    output("installed" => @home, "version" => HarnessCLI::VERSION, "engine" => config["engine"],
           "next" => "Run setup discover, configure relevant checks, then start a mode.",
           "adapter_snippet" => "Read .harness/docs/AGENT_ADAPTER.md. Use the same CLI workflow for chat and UI tasks. Return fixed help verbatim.")
    0
  end

  def dispatch
    return configure_project if @command == "configure"
    return inspect_engine if @command == "inspect-engine"
    if @command == "check-engine"
      problems = engine_issues
      output("engine" => project_config["engine"], "issues" => problems, "verified" => problems.empty?)
      return problems.empty? ? 0 : 1
    end
    return history if @command.start_with?("runs-")
    if @command == "questions"
      load_task
      schema = read(File.join(@home, "config/questions.json"))
      records = answer_records
      schema["questions"].each do |question|
        question["suggested_answer"] = question["recommended_by_mode"][@task["mode"]] if question["recommended_by_mode"]
        record = records[question["id"]]
        question["recorded_answer"] = record&.fetch("option")
        question["answer_source"] = record && record.fetch("source", { "kind" => "unrecorded" })
      end
      missing = schema["questions"].select { |question| question["required"] && !records.key?(question["id"]) }
      output(schema.merge("answers" => records.transform_values { |row| row["option"] },
                          "unanswered_questions" => missing, "missing_required" => missing.map { |question| question["id"] }))
      return 0
    end
    if @command.start_with?("task-")
      load_task
      return lifecycle_command
    end
    super
  end

  def inspect_engine
    file = @options.fetch(:file) { raise "Supply --file with the proposed engine instructions and version" }
    proposed = read(File.expand_path(file, @project))
    instruction = proposed.fetch("instructions_path")
    version = proposed.fetch("version").to_s.strip
    raise "Enter a version for these instructions" if version.empty?
    names = proposed.fetch("files", [])
    raise "Additional engine files must be an array" unless names.is_a?(Array)
    included = ([instruction] + names).uniq.to_h do |name|
      path = bounded(@project, name)
      raise "Choose an instruction file inside the project" unless File.file?(path)
      [path.delete_prefix(@project + File::SEPARATOR).tr("\\", "/"), file_hash(path)]
    end
    main = included.keys.first
    output("version" => version, "instructions_path" => main, "sha256" => included.fetch(main),
           "included_files" => included, "included_files_sha256" => hash_object(included))
    0
  end

  def configure_project
    raise "Setup configuration applies to the selected project. Omit --id." if @options[:id]
    file = @options.fetch(:file) { raise "Supply --file with the proposed project configuration JSON" }
    candidate = read(File.expand_path(file, @project))
    raise "Configuration must be an object with schema_version=1" unless candidate.is_a?(Hash) && candidate["schema_version"] == 1
    current_path = File.join(@home, "project.json")
    current = File.binread(current_path)
    id_path = File.join(@home, "active_task")
    if File.file?(id_path)
      load_task
      if verified_lifecycle["status"] == "active"
        raise "Execution configuration is frozen after the first baseline check" if File.file?(File.join(@task_dir, "execution_config.json"))
        installed = JSON.parse(current)
        if %w[engine agent execution].any? { |key| candidate[key] != installed[key] }
          raise "Engine, model, and repair budget cannot change during an active task"
        end
      end
    end
    execution = candidate.fetch("execution", { "max_repair_rounds" => 3 })
    limit = execution["max_repair_rounds"]
    raise "max_repair_rounds must be an integer from 1 to 20" unless limit.is_a?(Integer) && (1..20).cover?(limit)
    candidate["execution"] = execution
    agent = candidate["agent"]
    unless agent.is_a?(Hash) && agent["model"].is_a?(String) && !agent["model"].strip.empty? && %w[low medium high xhigh].include?(agent["effort"])
      raise "Set the agent model and a supported reasoning effort"
    end
    # Validate through the same readers without leaving an invalid configuration installed.
    begin
      File.write(current_path, JSON.pretty_generate(candidate) + "\n")
      problems = engine_issues
      raise problems.join("; ") unless problems.empty?
      checks
      benchmark_contract if candidate.fetch("checks", []).any? { |check| check["benchmark"] }
    rescue StandardError
      File.binwrite(current_path, current)
      raise
    end
    output("configured" => true, "project" => @project, "config_sha256" => hash_object(candidate),
           "engine" => candidate["engine"], "checks" => candidate.fetch("checks", []), "execution" => execution)
    0
  end

  def engine_issues
    issues = super
    engine = project_config.fetch("engine", {})
    included = engine.fetch("included_files", {})
    return issues + ["Engine included_files must map paths to SHA-256 values"] unless included.is_a?(Hash)
    unless included.empty?
      issues << "Engine included-file manifest changed" unless engine["included_files_sha256"] == hash_object(included)
      included.each do |name, expected|
        path = File.expand_path(name, @project)
        issues << "Engine included file changed or missing: #{name}" unless File.file?(path) && file_hash(path) == expected
      end
    end
    issues
  end

  def start_task
    raise "A new task receives a new ID. Omit --id." if @options[:id]
    id_path = File.join(@home, "active_task")
    if File.file?(id_path)
      load_task
      raise "An active task already owns this workspace. Pause, stop, or finish it first." if verified_lifecycle["status"] == "active"
    end
    @task = nil
    super
  end

  def load_task
    return super unless @options[:id]
    id = @options[:id]
    raise "Invalid task ID" unless id.match?(/\A[\w-]+\z/)
    @task_dir = bounded(File.join(@home, "tasks"), id)
    @task = read(File.join(@task_dir, "task.json"))
  end

  def lifecycle(dir = @task_dir)
    folder = File.join(dir, "lifecycle")
    begin
      names = Dir.children(folder)
    rescue Errno::ENOENT
      names = []
    end
    latest = names.select { |name| name.end_with?(".json") }.max
    unless latest
      started = { "status" => "active", "reason" => "Task started" }
      started["at"] = @task["created_at"] if dir == @task_dir && @task
      return started
    end
    record = read(File.join(folder, latest))
    unless record.is_a?(Hash) && %w[active paused stopped completed].include?(record["status"])
      raise ArgumentError, "Invalid task status in #{latest}"
    end
    record
  rescue SystemCallError, JSON::ParserError, ArgumentError => error
    { "status" => "unverified", "reason" => "Cannot read task lifecycle in #{folder}. #{error.message}" }
  end

  def verified_lifecycle(dir = @task_dir)
    record = lifecycle(dir)
    raise record["reason"] if record["status"] == "unverified"
    record
  end

  def repair_rounds
    matching_paths(@task_dir, "repair_rounds/*.json").length
  end

  def repair_limit
    @task.fetch("execution", {}).fetch("max_repair_rounds", 3)
  end

  def lifecycle_command
    status = verified_lifecycle["status"]
    case @command
    when "task-finish"
      raise "Only an active task can finish" unless status == "active"
      report = current_status
      raise "Task cannot finish: " + report["issues"].join("; ") unless report["verdict"] == "pass"
      write_lifecycle("completed", "All recorded completion gates passed")
    when "task-pause", "task-stop"
      reason = @options[:reason].to_s.strip
      raise "Supply --reason with the user decision" if reason.empty?
      raise "Only an active task can pause or stop" unless status == "active"
      write_lifecycle(@command == "task-pause" ? "paused" : "stopped", reason)
    when "task-resume"
      raise "Only a paused or stopped task can resume" unless %w[paused stopped].include?(status)
      raise "This task exhausted its repair budget. Start a new task for more repairs." if lifecycle["cause"] == "repair_limit"
      selected = @task["id"]
      pointer = File.join(@home, "active_task")
      if File.file?(pointer) && File.read(pointer).strip != selected
        other = File.read(pointer).strip
        other_dir = bounded(File.join(@home, "tasks"), other)
        other_state = verified_lifecycle(other_dir)["status"]
        raise "Another active task owns this workspace" if other_state == "active"
      end
      write_lifecycle("active", @options[:reason].to_s.empty? ? "Explicit resume request" : @options[:reason])
      File.write(pointer, selected)
    when "task-repair-round"
      require_agreement
      maximum = repair_limit
      reason = @options[:reason].to_s.strip
      raise "Supply --reason with the verified findings to repair" if reason.empty?
      if repair_rounds >= maximum
        write_lifecycle("stopped", reason, cause: "repair_limit")
        raise "Repair round limit reached. Task stopped. Report the remaining issues."
      end
      immutable(File.join(@task_dir, "repair_rounds", unique_id + ".json"),
                "round" => repair_rounds + 1, "reason" => reason, "snapshot_sha256" => source_snapshot["snapshot_sha256"], "at" => Time.now.utc.iso8601)
    else
      raise "Unknown task command"
    end
    output("task_id" => @task["id"], "lifecycle" => lifecycle, "repair_rounds" => repair_rounds)
    0
  end

  def write_lifecycle(status, reason, cause: nil)
    immutable(File.join(@task_dir, "lifecycle", unique_id + ".json"),
              "status" => status, "reason" => reason, "cause" => cause, "at" => Time.now.utc.iso8601,
              "snapshot_sha256" => source_snapshot["snapshot_sha256"])
  end

  def agreement_issues
    issues = super
    %w[execution engine agent].each do |key|
      issues << "Task #{key} changed since agreement" if @task[key] != project_config[key]
    end
    issues << "Task is #{lifecycle['status']}; resume before implementation or checks" if %w[paused stopped].include?(lifecycle["status"])
    issues
  end

  def answer
    raise "Only an active task accepts answers" unless verified_lifecycle["status"] == "active"
    id = @argv.first
    if id != "notes" && File.file?(File.join(@task_dir, "execution_config.json"))
      raise "Task permissions are frozen once checks start. Start a new task for a different agreement."
    end
    super
  end

  def require_agreement
    verified_lifecycle
    super
    raise "Task is completed. Start a new task before implementation or checks." if lifecycle["status"] == "completed"
  end

  def current_status
    state = lifecycle
    if state["status"] == "unverified"
      return @task.slice("mode", "action", "scope").merge("task_id" => @task["id"], "lifecycle" => state,
        "verdict" => "unverified", "issues" => [state["reason"]], "artifact_directory" => @task_dir)
    end
    report = super
    report.merge("lifecycle" => state, "repair_rounds" => repair_rounds,
                 "max_repair_rounds" => repair_limit,
                 "artifact_directory" => @task_dir)
  end

  def verify
    if lifecycle["status"] == "unverified"
      output(current_status)
      return 1
    end
    super
  end

  def history
    paths = matching_paths(@home, "tasks/*/task.json").sort.reverse
    records = paths.map do |path|
      task = read(path)
      dir = File.dirname(path)
      { "id" => task["id"], "mode" => task["mode"], "action" => task["action"], "scope" => task["scope"],
        "created_at" => task["created_at"], "lifecycle" => lifecycle(dir),
        "initial_snapshot_sha256" => task.dig("initial_snapshot", "snapshot_sha256"), "artifact_directory" => dir }
    end
    case @command
    when "runs-list"
      output("runs" => records)
    when "runs-show", "runs-compare"
      ids = @argv
      required = @command == "runs-show" ? 1 : 2
      raise "Supply #{required} task ID(s)" unless ids.length == required
      selected = ids.map do |id|
        record = records.find { |row| row["id"] == id }
        raise "Unknown task: #{id}" unless record
        dir = record["artifact_directory"]
        @task_dir = dir
        ledger_issues = integrity_issues
        ledger_issues << record["lifecycle"]["reason"] if record["lifecycle"]["status"] == "unverified"
        check_receipts = matching_paths(dir, "results/**/result.json").sort.map do |path|
          check = read(path)
          %w[stdout stderr].each do |stream|
            log = File.join(File.dirname(path), stream + ".log")
            next unless File.file?(log)
            check[stream + "_path"] = log.delete_prefix(File.join(@home, "tasks") + File::SEPARATOR)
            preview = File.binread(log, 4096)
            check[stream + "_preview"] = preview ? preview.force_encoding("UTF-8").scrub : ""
            check[stream + "_truncated"] = File.size(log) > 4096
          end
          check
        end
        record.merge("task" => read(File.join(dir, "task.json")),
                     "ledger" => { "verdict" => ledger_issues.empty? ? "pass" : "revise", "issues" => ledger_issues },
                     "benchmark" => File.file?(File.join(dir, "benchmark/contract.json")) ? read(File.join(dir, "benchmark/contract.json")) : nil,
                     "repair_rounds" => matching_paths(dir, "repair_rounds/*.json").sort.map { |path| read(path) },
                     "answers" => matching_paths(dir, "answers/*.json").sort.map { |path| read(path) },
                     "checks" => check_receipts,
                     "reviews" => matching_paths(dir, "reviews/*.json").sort.map { |path| read(path) },
                     "reports" => matching_paths(dir, "reports/*.json").sort.map { |path| read(path) })
      end
      output(@command == "runs-show" ? selected.first : { "runs" => selected, "note" => "Compare scope and contracts before interpreting values. No combined score is defined." })
    else
      raise "Choose runs list, show, or compare"
    end
    0
  end

  def prompt
    result = super
    puts "\nDECISION SOURCES"
    answer_records.each do |question, record|
      puts "#{question}: #{JSON.generate(record.fetch('source', { 'kind' => 'unrecorded' }))}"
    end
    puts "\nWORKFLOW CONTROL\nUse the same task ID from CLI, chat, and UI. Record each repair with task repair-round before editing. The configured round limit is #{repair_limit}. Stop and report when the limit is reached. Run task finish only after current checks and review pass. Do not invent permissions or verification."
    result
  end

  def output(value)
    @benchmark_results ? @benchmark_results << value : super
  end

  def benchmark
    @benchmark_results = []
    result = super
    captured = @benchmark_results
    @benchmark_results = nil
    output("checks" => captured, "verdict" => result.zero? ? "pass" : "revise")
    result
  ensure
    @benchmark_results = nil
  end
end

HarnessCLI.prepend(WorkbenchCommands)
