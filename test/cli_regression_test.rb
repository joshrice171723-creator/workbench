require 'minitest/autorun'
require 'tmpdir'
require 'fileutils'
require 'json'
require 'open3'
require 'rbconfig'
require 'digest'
require_relative '../lib/harness'
require_relative '../lib/workbench'
$stdout.sync = true

class PortableCliTest < Minitest::Test
  PACKAGE = File.expand_path('..', __dir__)
  CLI = File.join(PACKAGE, 'bin', 'harness')
  CliStatus = Struct.new(:exitstatus) do
    def success?
      exitstatus == 0
    end
  end

  def setup
    @project = Dir.mktmpdir('portable-cli-test-')
    write('AGENTS.md', "Preserve this project rule.\n")
    write('backend/Gemfile', "source 'https://rubygems.org'\ngem 'rails'\n")
    write('frontend/package.json', JSON.generate(name: 'generic-client', scripts: {test: 'node --test'}, dependencies: {react: '*'}))
    write('backend/app/models/customer.rb', "class Customer; end\n")
    write('backend/db/migrate/001_create_customers.rb', "class CreateCustomers; end\n")
    write('checks/behavior.rb', "puts 'Behavior check executed'\n")
    write('checks/workload.rb', "puts 'Workload: 3 records; 0 writes'\n")
    write('coding-rules.md', "Use plain Active Record. Preserve behavior.\n")
    @original_rules = File.binread(path('AGENTS.md'))
  end

  def teardown
    FileUtils.remove_entry(@project) if @project && File.directory?(@project)
  end

  def path(relative)
    File.join(@project, relative)
  end

  def write(relative, content)
    FileUtils.mkdir_p(File.dirname(path(relative)))
    File.binwrite(path(relative), content)
  end

  def cli(*args)
    result = nil
    stdout, stderr = capture_io { result = HarnessCLI.new([*args, '--project', @project]).call }
    [stdout + stderr, CliStatus.new(result)]
  end

  def succeeds(*args)
    output, status = cli(*args)
    assert status.success?, "Command #{args.inspect} failed: #{output}"
    output
  end

  def fails(*args)
    output, status = cli(*args)
    refute status.success?, "Command #{args.inspect} unexpectedly succeeded: #{output}"
    output
  end

  def configure
    succeeds('init')
    file = path('.harness/project.json')
    config = JSON.parse(File.read(file))
    config['engine'] = {'instructions_path' => 'coding-rules.md', 'sha256' => Digest::SHA256.file(path('coding-rules.md')).hexdigest}
    config['checks'] = [
      {'name' => 'behavior', 'argv' => [RbConfig.ruby, 'checks/behavior.rb'], 'cwd' => '.', 'required' => true, 'benchmark' => false},
      {'name' => 'workload', 'argv' => [RbConfig.ruby, 'checks/workload.rb'], 'cwd' => '.', 'required' => true, 'benchmark' => true}
    ]
    config['benchmark'] = {'name' => 'Generic workload', 'version' => '1', 'cases' => ['fixed result', 'unrelated growth'],
      'workload' => 'Synthetic customer data', 'metrics' => ['records', 'writes'],
      'assertions' => ['Preserve identities'], 'checks' => ['workload'], 'scripts' => ['checks/workload.rb']}
    File.write(file, JSON.pretty_generate(config))
    config
  end

  def start(mode = 'refactor')
    succeeds('start', mode, '--action', 'Simplify the customer workflow', '--scope', 'backend/app/models')
    {'scope_confirmed' => 'confirmed', 'code_changes' => mode == 'audit' ? 'deny' : 'allow_scoped',
      'migrations' => 'deny', 'dependencies' => 'deny', 'engine_integration' => 'confirmed'}.each do |question, answer|
      succeeds('answer', question, answer)
    end
  end

  def records
    Dir.glob(path('.harness/tasks/**/*')).select { |f| File.file?(f) && %w[.json .log].include?(File.extname(f)) }
      .map { |f| File.read(f) }.join("\n")
  end

  def complete_checks
    succeeds('run', 'behavior', '--phase', 'baseline')
    succeeds('benchmark', '--phase', 'baseline')
    succeeds('run', 'behavior', '--phase', 'after')
    succeeds('benchmark', '--phase', 'after')
    snapshot = JSON.parse(succeeds('status')).fetch('snapshot_sha256')
    criteria = %w[correctness human_readability maintainability performance failure_handling scope_and_constraints]
    review = {'schema_version' => 1, 'snapshot_sha256' => snapshot, 'verdict' => 'pass', 'author' => 'agent',
      'criteria' => criteria.map { |id| {'id' => id, 'verdict' => 'pass', 'evidence' => ['Synthetic review fixture for orchestration tests.']} }}
    file = path('.harness/synthetic-review.json')
    File.write(file, JSON.pretty_generate(review))
    succeeds('review', '--file', file)
  end

  def test_help_is_fixed_and_does_not_require_project_setup
    first = succeeds('help')
    assert_equal first, succeeds('help')
    %w[audit refactor feature build].each { |mode| assert_includes first, mode }
    refute Dir.exist?(path('.harness'))
    stdout, stderr, status = Open3.capture3(RbConfig.ruby, CLI, 'help')
    assert status.success?, stderr
    assert_equal first, stdout
  end

  def test_installation_preserves_existing_agent_rules_and_refuses_overwrite
    succeeds('init')
    assert_equal @original_rules, File.binread(path('AGENTS.md'))
    before = File.binread(path('.harness/project.json'))
    fails('init')
    assert_equal before, File.binread(path('.harness/project.json'))
  end

  def test_discovery_does_not_install_dependencies_or_change_project
    original = Dir.glob(path('**/*')).select { |f| File.file?(f) }.to_h { |f| [f, Digest::SHA256.file(f).hexdigest] }
    output = succeeds('discover')
    assert_match(/backend|rails/i, output)
    assert_match(/frontend|react/i, output)
    original.each { |f, hash| assert_equal hash, Digest::SHA256.file(f).hexdigest }
    refute Dir.exist?(path('frontend/node_modules'))
  end

  def test_unconfigured_task_cannot_pass
    succeeds('init')
    succeeds('start', 'refactor', '--action', 'Simplify code', '--scope', 'backend')
    fails('verify')
  end

  def test_task_without_recorded_checks_cannot_pass
    configure
    start
    fails('verify')
  end

  def test_new_migration_cannot_pass_refactor
    configure
    start
    write('backend/db/migrate/002_add_index.rb', "class AddIndex; end\n")
    output = fails('verify')
    assert_includes output, 'Forbidden migrations change'
  end

  def test_modified_migration_cannot_pass_refactor
    configure
    start
    write('backend/db/migrate/001_create_customers.rb', "class ChangedMigration; end\n")
    assert_includes fails('verify'), 'Forbidden migrations change'
  end

  def test_deleted_migration_cannot_pass_refactor
    configure
    start
    File.unlink(path('backend/db/migrate/001_create_customers.rb'))
    assert_includes fails('verify'), 'Forbidden migrations change'
  end

  def test_changed_dependency_manifest_cannot_pass_refactor
    configure
    start
    write('frontend/package.json', JSON.generate(dependencies: {react: '*', extra: '*'}))
    assert_includes fails('verify'), 'Forbidden dependencies change'
  end

  def test_audit_does_not_authorize_app_edits
    configure
    start('audit')
    write('backend/app/models/customer.rb', "class Customer; def changed; true; end; end\n")
    fails('verify')
  end

  def test_real_command_results_record_output_and_failure
    configure
    start
    succeeds('run', 'behavior', '--phase', 'baseline')
    assert_includes records, 'Behavior check executed'
    write('checks/behavior.rb', "warn 'Deliberate check failure'; exit 7\n")
    fails('run', 'behavior', '--phase', 'after')
    assert_includes records, 'Deliberate check failure'
    fails('verify')
  end

  def test_benchmark_requires_baseline_before_after
    configure
    start
    fails('benchmark', '--phase', 'after')
  end

  def test_benchmark_rejects_changed_workload_script
    configure
    start
    succeeds('benchmark', '--phase', 'baseline')
    write('checks/workload.rb', "puts 'A weaker workload'\n")
    fails('benchmark', '--phase', 'after')
  end

  def test_benchmark_rejects_changed_assertions
    configure
    start
    succeeds('benchmark', '--phase', 'baseline')
    file = path('.harness/project.json')
    config = JSON.parse(File.read(file))
    config['benchmark']['assertions'] = ['Skip identity checks']
    File.write(file, JSON.pretty_generate(config))
    fails('benchmark', '--phase', 'after')
  end

  def test_engine_instruction_drift_is_reported
    configure
    start
    write('coding-rules.md', "Use a different standard.\n")
    fails('prompt')
    fails('verify')
  end

  def test_check_cannot_escape_the_project_working_directory
    configure
    file = path('.harness/project.json')
    config = JSON.parse(File.read(file))
    config['checks'][0]['cwd'] = '..'
    File.write(file, JSON.pretty_generate(config))
    start
    fails('run', 'behavior', '--phase', 'baseline')
  end

  def test_shell_command_string_is_not_accepted_as_argv
    configure
    file = path('.harness/project.json')
    config = JSON.parse(File.read(file))
    config['checks'][0]['argv'] = 'echo should-not-run'
    File.write(file, JSON.pretty_generate(config))
    start
    fails('run', 'behavior', '--phase', 'baseline')
  end

  def test_benchmark_baseline_does_not_accept_a_changed_starting_app
    configure
    start
    write('backend/app/models/customer.rb', "class Customer; def modified; true; end; end\n")
    fails('benchmark', '--phase', 'baseline')
  end

  def test_complete_evidence_can_pass_then_app_change_invalidates_it
    configure
    start
    complete_checks
    report = JSON.parse(succeeds('verify'))
    assert_equal 'pass', report.fetch('verdict')
    write('backend/app/models/customer.rb', "class Customer; def modified; true; end; end\n")
    stale = fails('verify')
    assert_match(/stale|snapshot|changed|revision/i, stale)
  end

  def test_evidence_tampering_cannot_preserve_a_pass
    configure
    start
    complete_checks
    succeeds('verify')
    evidence = Dir.glob(path('.harness/tasks/**/stdout.log')).first
    refute_nil evidence
    File.write(evidence, 'Fabricated output')
    fails('verify')
  end

  def test_required_check_cannot_be_removed_after_baseline
    configure
    start
    succeeds('run', 'behavior', '--phase', 'baseline')
    file = path('.harness/project.json')
    config = JSON.parse(File.read(file))
    config['checks'].reject! { |check| check['name'] == 'behavior' }
    File.write(file, JSON.pretty_generate(config))
    assert_match(/config|check|changed|drift/i, fails('benchmark', '--phase', 'baseline'))
    fails('verify')
  end

  def test_one_element_argv_cannot_invoke_a_shell
    configure
    file = path('.harness/project.json')
    config = JSON.parse(File.read(file))
    config['checks'][0]['argv'] = ['echo unsafe > shell-marker.txt']
    File.write(file, JSON.pretty_generate(config))
    start
    fails('run', 'behavior', '--phase', 'baseline')
    refute File.exist?(path('shell-marker.txt'))
  end

  def test_build_mode_honors_explicit_denial_of_schema_and_dependency_changes
    configure
    start('build')
    write('backend/db/migrate/002_add_index.rb', "class AddIndex; end\n")
    write('frontend/package.json', JSON.generate(dependencies: {react: '*', extra: '*'}))
    output = fails('verify')
    assert_includes output, 'Forbidden migrations change'
    assert_includes output, 'Forbidden dependencies change'
  end

  def test_unregistered_review_cannot_preserve_a_pass
    configure
    start
    complete_checks
    succeeds('verify')
    task_dir = Dir.glob(path('.harness/tasks/*')).find { |file| File.directory?(file) }
    review_dir = File.join(task_dir, 'reviews')
    original_review = Dir.glob(File.join(review_dir, '*.json')).first
    refute_nil original_review
    FileUtils.cp(original_review, File.join(review_dir, 'zzzz-injected-review.json'))
    assert_match(/ledger|unregistered|integrity|record/i, fails('verify'))
  end
end
