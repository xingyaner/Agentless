import sys
import time
import signal
import errno
import docker
import json
import os
import platform
import re
import resource
import traceback
import subprocess
from tqdm import tqdm
from typing import Optional, Dict, Any, List
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, Dict, Any  # 确保导入了 Any
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm

# =================================================================
# --- 【隔离适配层】定义 Mock 占位符，防止 NameError ---
# =================================================================
class TestSpec:
    def __init__(self, *args, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)
# 定义原代码函数签名中引用的所有类名和常量
SWEbenchInstance = Any
FAIL_TO_PASS = "FAIL_TO_PASS"
KEY_INSTANCE_ID = "instance_id"
MAP_REPO_VERSION_TO_SPECS = {}
PASS_TO_PASS = "PASS_TO_PASS"
USE_X86 = []

def build_env_images(*args, **kwargs): pass
def get_dataset_from_preds(*args, **kwargs): return []
def run_instance(*args, **kwargs): pass
def make_env_script_list(*args, **kwargs): return []
def make_repo_script_list(*args, **kwargs): return []
def get_test_directives(*args, **kwargs): return []

def extract_resolved_info(directory_path):
    return {}

def rearrange_patches(test_specs):
    return test_specs

def make_regression_spec(instance: SWEbenchInstance) -> TestSpec:
    """补全被引用的定义"""
    if isinstance(instance, TestSpec):
        return instance
    return TestSpec(instance_id=instance.get(KEY_INSTANCE_ID, "unknown"))

def make_reproduction_sec(instance: SWEbenchInstance) -> TestSpec:
    """补全被引用的定义"""
    if isinstance(instance, TestSpec):
        return instance
    return TestSpec(instance_id=instance.get(KEY_INSTANCE_ID, "unknown"))

OPEN_FILE_LIMIT = 4096

NOOP_PATCH = """diff --git a/this_is_invisible.py b/this_is_invisible.py
new file mode 100644
index 0000000..e69de29
--- /dev/null
+++ b/this_is_invisible.py
@@ -0,0 +1 @@
+# This is a commented out line
"""

NOOP_PATCH_2 = """diff --git a/this_is_invisible_2.py b/this_is_invisible_2.py
new file mode 100644
index 0000000..e69de29
--- /dev/null
+++ b/this_is_invisible_2.py
@@ -0,0 +1 @@
+# This is a commented out line
"""

import re
import os
import subprocess
from typing import Optional


def _auto_discover_project_symbols(binary_path: str, project_name: str) -> Optional[List[str]]:
    """启发式查找项目特有符号"""
    try:
        result = subprocess.run(['nm', '-D', binary_path], capture_output=True, text=True, errors='ignore')
        if result.returncode != 0:
            result = subprocess.run(['nm', binary_path], capture_output=True, text=True, errors='ignore')

        lines = result.stdout.splitlines()
        keywords = [project_name.lower(), "deflate", "inflate", "adler32", "crc32"] if project_name == "zlib" else [
            project_name.lower()]
        boilerplate = ('__asan', '__lsan', '__ubsan', '__sanitizer', 'fuzzer::', 'LLVM', 'afl_', '_Z', 'std::')

        candidates = []
        for line in lines:
            parts = line.split()
            if not parts: continue
            symbol = parts[-1]
            if any(kw in symbol.lower() for kw in keywords) and not symbol.startswith(boilerplate):
                candidates.append(symbol)
        return candidates[:5] if candidates else None
    except:
        return None


def _cleanup_environment(oss_fuzz_path: str, project_name: str):
    """环境净化机制：清理残留容器并释放文件句柄，防止 Text file busy 错误"""
    print(f"[*] Pre-build cleanup for project: {project_name}")
    try:
        # 杀死相关 Docker 容器
        kill_cmd = f"docker ps -q --filter \"ancestor=gcr.io/oss-fuzz/{project_name}\" | xargs -r docker kill"
        subprocess.run(kill_cmd, shell=True, capture_output=True)
        kill_runner_cmd = "docker ps -q --filter \"ancestor=gcr.io/oss-fuzz-base/base-runner\" | xargs -r docker kill"
        subprocess.run(kill_runner_cmd, shell=True, capture_output=True)
    except Exception as e:
        print(f"[!] Warning during docker cleanup: {e}")

    out_dir = os.path.join(oss_fuzz_path, "build", "out", project_name)
    if os.path.exists(out_dir):
        max_retries = 3
        for i in range(max_retries):
            busy_files = False
            try:
                for f in os.listdir(out_dir):
                    if not f.endswith(('.so', '.a', '.zip', '.dict', '.options', '.txt')):
                        f_path = os.path.join(out_dir, f)
                        if os.path.isfile(f_path):
                            try:
                                os.remove(f_path)
                            except OSError as e:
                                if e.errno == errno.ETXTBSY: busy_files = True
                if not busy_files: break
            except Exception:
                pass
            if busy_files and i < max_retries - 1:
                print(f"[!] Files busy in {out_dir}, retrying cleanup ({i + 1}/{max_retries})...")
                time.sleep(2)


def run_fuzz_build_and_validate(
        project_name: str,
        oss_fuzz_path: str,
        sanitizer: str,
        engine: str,
        architecture: str,
        mount_path: Optional[str] = None
) -> dict:
    """执行构建并运行 6 步深度验证逻辑"""
    print(f"--- Tool: run_fuzz_build_and_validate called for: {project_name} ---")
    _cleanup_environment(oss_fuzz_path, project_name)

    LOG_FILE_PATH = "fuzz_build_log_file/fuzz_build_log.txt"
    os.makedirs("fuzz_build_log_file", exist_ok=True)

    report = {
        "step_1_static_output": {"status": "pending", "details": ""},
        "step_2_sanitizer_injected": {"status": "pending", "details": ""},
        "step_3_engine_linked": {"status": "pending", "details": ""},
        "step_4_logic_linked": {"status": "pending", "details": ""},
        "step_5_dependencies_ok": {"status": "pending", "details": ""},
        "step_6_runtime_stability": {"status": "pending", "details": ""}
    }

    try:
        helper_path = os.path.join(oss_fuzz_path, "infra/helper.py")
        command = ["python3.10", helper_path, "build_fuzzers"]
        if mount_path:
            command.extend([project_name, mount_path])
        command.extend(["--sanitizer", sanitizer, "--engine", engine, "--architecture", architecture])
        if not mount_path:
            command.append(project_name)

        print(f"--- [Phase 1] Executing Build Command ---")
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                                   cwd=oss_fuzz_path)

        full_log_content = []
        for line in process.stdout:
            print(line, end='', flush=True)
            full_log_content.append(line)
        process.wait()
        final_log = "".join(full_log_content)

        is_build_ok = (process.returncode == 0)
        if any(k in final_log.lower() for k in ["error:", "failed:", "build failed"]): is_build_ok = False
        if is_build_ok and "found 0 targets" in final_log.lower(): is_build_ok = False

        if is_build_ok:
            print(f"\n--- [Phase 2] Starting Deep Validation ---")
            out_dir = os.path.join(oss_fuzz_path, "build", "out", project_name)
            targets = []
            if os.path.exists(out_dir):
                ignore_ext = ('.so', '.a', '.jar', '.class', '.zip', '.dict', '.options')
                for f in os.listdir(out_dir):
                    f_path = os.path.join(out_dir, f)
                    if os.path.isfile(f_path) and os.access(f_path, os.X_OK):
                        if not f.startswith(('afl-', 'llvm-', 'jazzer')) and not f.endswith(ignore_ext):
                            targets.append(f)

            if not targets:
                is_build_ok = False
                report["step_1_static_output"] = {"status": "fail", "details": "No fuzz targets found."}
            else:
                target = targets[0]
                primary_path = os.path.join(out_dir, target)
                report["step_1_static_output"] = {"status": "pass", "details": f"Target: {target}"}

                nm_res = subprocess.run(['nm', primary_path], capture_output=True, text=True, errors='ignore')
                syms = nm_res.stdout
                report["step_2_sanitizer_injected"] = {"status": "pass" if "__asan" in syms else "fail", "details": "ASan found"}
                eng_key = "LLVMFuzzerRunDriver" if engine == "libfuzzer" else "__afl_"
                report["step_3_engine_linked"] = {"status": "pass" if eng_key in syms else "fail", "details": f"Engine {eng_key} found"}
                found_syms = _auto_discover_project_symbols(primary_path, project_name)
                report["step_4_logic_linked"] = {"status": "pass" if found_syms else "warning", "details": f"Discovered: {found_syms}"}

                ldd_cmd = ["python3.10", helper_path, "shell", project_name, "-c", f"ldd /out/{target}"]
                ldd_res = subprocess.run(ldd_cmd, cwd=oss_fuzz_path, capture_output=True, text=True, errors='ignore')
                report["step_5_dependencies_ok"] = {"status": "pass" if "not found" not in ldd_res.stdout.lower() else "fail", "details": "ldd check"}

                print(f"[*] Starting Stability Test for {target}...")
                test_env = os.environ.copy()
                test_env["AFL_NO_UI"] = "1"
                test_env["AFL_QUIET"] = "1"
                run_cmd = [sys.executable, helper_path, "run_fuzzer", "--engine", engine, "--sanitizer", sanitizer, project_name, target]
                if engine == "libfuzzer": run_cmd.extend(["--", "-max_total_time=30"])

                stability_proc = subprocess.Popen(run_cmd, cwd=oss_fuzz_path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, preexec_fn=os.setsid, env=test_env)

                has_exec_rate = False
                fuzzer_started = False
                start_run_time = None
                try:
                    while True:
                        line = stability_proc.stdout.readline()
                        if not line:
                            if stability_proc.poll() is not None: break
                            continue
                        if not fuzzer_started and any(m in line for m in ["INFO:", "[*] ", "fuzz target", "Entering main"]):
                            fuzzer_started = True
                            start_run_time = time.time()
                        if any(kw in line for kw in ["exec/s:", "exec speed", "corp:"]): has_exec_rate = True
                        if fuzzer_started and start_run_time and (time.time() - start_run_time > 40): break
                finally:
                    try: os.killpg(os.getpgid(stability_proc.pid), signal.SIGKILL)
                    except: pass
                    stability_proc.wait()

                report["step_6_runtime_stability"] = {"status": "pass" if has_exec_rate else "fail", "details": "Execution verified"}

        with open(LOG_FILE_PATH, "w", encoding="utf-8") as f:
            f.write("success" if is_build_ok else final_log)
        return {"status": "success" if is_build_ok else "error", "validation_report": report}
    except Exception as e:
        return {"status": "error", "message": str(e), "validation_report": report}


def apply_patch(solution_file_path: str) -> dict:
    """物理应用补丁，并返回修改规模"""
    applied_count = 0
    total_lines = 0
    try:
        if not os.path.exists(solution_file_path): return {"status": "error", "files": 0, "lines": 0}
        with open(solution_file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        blocks = content.split('---=== FILE ===---')[1:]
        for block in blocks:
            try:
                parts = block.split('---=== ORIGINAL ===---')
                f_path = parts[0].strip()
                c_parts = parts[1].split('---=== REPLACEMENT ===---')
                orig, repl = c_parts[0].strip("\n\r"), c_parts[1].strip("\n\r")
                if not os.path.exists(f_path): continue
                with open(f_path, 'r', encoding='utf-8') as f_in:
                    f_content = f_in.read()
                if orig in f_content:
                    with open(f_path, 'w', encoding='utf-8') as f_out:
                        f_out.write(f_content.replace(orig, repl, 1))
                    applied_count += 1
                    total_lines += len(repl.splitlines())
            except: continue
        return {"status": "success" if applied_count > 0 else "error", "files": applied_count, "lines": total_lines}
    except: return {"status": "error", "files": 0, "lines": 0}


def run_oss_fuzz_validation(instance_id: str, model_patch: str, project_metadata: dict) -> dict:
    """Agentless 最终裁判：修复成功的唯一标准是 Step 1 和 Step 6 通过"""
    temp_sol = "agentless_solution_temp.txt"
    with open(temp_sol, "w", encoding="utf-8") as f: f.write(model_patch)

    apply_res = apply_patch(temp_sol)
    if apply_res['status'] == "error":
        return {"success": False, "files": 0, "lines": 0}

    v_res = run_fuzz_build_and_validate(
        project_name=instance_id,
        oss_fuzz_path=os.path.abspath("./oss-fuzz"),
        sanitizer=project_metadata.get('sanitizer', 'address'),
        engine=project_metadata.get('engine', 'libfuzzer'),
        architecture=project_metadata.get('architecture', 'x86_64'),
        mount_path=project_metadata.get('project_source_path')
    )

    report = v_res.get("validation_report", {})
    is_step1_pass = report.get("step_1_static_output", {}).get("status") == "pass"
    is_step6_pass = report.get("step_6_runtime_stability", {}).get("status") == "pass"
    truly_fixed = is_step1_pass and is_step6_pass

    print(f"\n" + "=" * 50)
    print(f"🔍 VALIDATION DIAGNOSTIC FOR: {instance_id}")
    # 修复 bool 对象没有 upper() 的问题
    print(f"  - Step 1 (Binary Exists): {str(is_step1_pass).upper()}")
    print(f"  - Step 6 (Runtime Stability): {str(is_step6_pass).upper()}")
    print(f"  - FINAL DECISION: {'SUCCESS' if truly_fixed else 'FAILURE'}")
    print("=" * 50 + "\n")

    return {"success": truly_fixed, "files": apply_res['files'], "lines": apply_res['lines']}


def remove_ansi_sequences(input_string):
    ansi_escape_pattern = r"\x1b\[\d+m"
    clean_string = re.sub(ansi_escape_pattern, "", input_string)

    return clean_string


def txt_file_contains_string(path_to_txt, expected_output, other_patterns=[]):
    """
    Check if the given text file contains the specified string.
    :param path_to_txt: Path to the text file.
    :param expected_output: The string to search for in the text file.
    :return: True if the string is found in the text file, otherwise False.
    """
    try:
        with open(path_to_txt, "r", encoding="utf-8") as file:
            content = file.read()
            filtered_content = remove_ansi_sequences(content)
            for pattern in other_patterns:
                if pattern in filtered_content:
                    return False
            return expected_output in filtered_content

    except FileNotFoundError:
        pass
    except IOError:
        print(f"An error occurred while reading the file at {path_to_txt}.")

    return False


def create_instance_test_dict(jsonl_file_path):
    instance_test_dict = {}

    with open(jsonl_file_path, "r") as file:
        for line in file:
            json_obj = json.loads(line.strip())
            instance_id = json_obj["instance_id"]
            test_patch = json_obj["test_patch"]
            instance_test_dict[instance_id] = test_patch

    return instance_test_dict


def extract_resolved_info(directory_path):
    # Check if the directory exists
    if not os.path.exists(directory_path) or not os.path.isdir(directory_path):
        return {}

    result = {}
    for subdir in os.listdir(directory_path):
        subdir_path = os.path.join(directory_path, subdir)
        if os.path.isdir(subdir_path):
            report_path = os.path.join(subdir_path, "report.json")
            if os.path.isfile(report_path):
                try:
                    with open(report_path, "r", encoding="utf-8") as report_file:
                        data = json.load(report_file)
                        resolved_value = data.get(subdir, {}).get("resolved", False)
                        result[subdir] = resolved_value
                except (json.JSONDecodeError, KeyError):
                    result[subdir] = False
            # else:
            #     result[subdir] = False
    return result


def make_reproduction_sec(instance: SWEbenchInstance) -> TestSpec:
    if isinstance(instance, TestSpec):
        return instance
    instance_id = instance[KEY_INSTANCE_ID]
    repo = instance["repo"]
    version = instance["version"]
    base_commit = instance["base_commit"]
    production_test = instance["production_test"]

    def _from_json_or_obj(key: str) -> Any:
        """If key points to string, load with json"""
        if isinstance(instance[key], str):
            return json.loads(instance[key])
        return instance[key]

    pass_to_pass = _from_json_or_obj(PASS_TO_PASS)
    fail_to_pass = _from_json_or_obj(FAIL_TO_PASS)

    env_name = "testbed"
    repo_directory = f"/{env_name}"
    specs = MAP_REPO_VERSION_TO_SPECS[repo][version]

    repo_script_list = make_repo_script_list(
        specs, repo, repo_directory, base_commit, env_name
    )
    env_script_list = make_env_script_list(instance, specs, env_name)
    eval_script_list = make_reproduction_script_list(
        instance, specs, env_name, repo_directory, base_commit, production_test
    )
    if platform.machine() in {"aarch64", "arm64"}:
        # use arm64 unless explicitly specified
        arch = "arm64" if instance_id not in USE_X86 else "x86_64"
    else:
        arch = "x86_64"

    return TestSpec(
        instance_id=instance_id,
        repo=repo,
        env_script_list=env_script_list,
        repo_script_list=repo_script_list,
        eval_script_list=eval_script_list,
        version=version,
        arch=arch,
        FAIL_TO_PASS=fail_to_pass,
        PASS_TO_PASS=pass_to_pass,
    )


def make_reproduction_script_list(
    instance, specs, env_name, repo_directory, base_commit, reproduce_patch
):
    """
    Applies new production tests and run tests
    """
    # Reset test files to the state they should be in before the patch.
    reset_tests_command = f"git checkout {base_commit}"

    HEREDOC_DELIMITER = "EOF_114329324912"
    fake_apply_test_patch_command = (
        f"git apply -v - <<'{HEREDOC_DELIMITER}'\n{NOOP_PATCH_2}\n{HEREDOC_DELIMITER}"
    )

    apply_reproduce_test_command = f"git apply -v - <<'{HEREDOC_DELIMITER}'\n{reproduce_patch}\n{HEREDOC_DELIMITER}"
    reproduce_test_command = "python3 reproduce_bug.py"

    eval_commands = [
        f"source /opt/miniconda3/bin/activate",
        f"conda activate {env_name}",
        f"cd {repo_directory}",
    ]
    if "eval_commands" in specs:
        eval_commands += specs["eval_commands"]
    eval_commands += [
        f"git config --global --add safe.directory {repo_directory}",  # for nonroot user
        f"cd {repo_directory}",
        # This is just informational, so we have a record
        f"git status",
        f"git show",
        f"git diff {base_commit}",
        "source /opt/miniconda3/bin/activate",
        f"conda activate {env_name}",
    ]
    if "install" in specs:
        eval_commands.append(specs["install"])
    eval_commands += [
        reset_tests_command,
        fake_apply_test_patch_command,  # If we don't apply some sort of patch the harness won't return the tests which passed
        apply_reproduce_test_command,
        reproduce_test_command,
        # reset_tests_command,
    ]
    return eval_commands


def make_reproduction_sec(instance: SWEbenchInstance) -> TestSpec:
    if isinstance(instance, TestSpec):
        return instance
    return TestSpec(instance_id=instance.get(KEY_INSTANCE_ID))

def make_regression_script_list(instance, specs, env_name, repo_directory, base_commit):
    """
    Applies the test patch and runs the tests.
    """
    # Reset test files to the state they should be in before the patch.
    reset_tests_command = f"git checkout {base_commit}"

    HEREDOC_DELIMITER = "EOF_114329324912"
    fake_apply_test_patch_command = (
        f"git apply -v - <<'{HEREDOC_DELIMITER}'\n{NOOP_PATCH_2}\n{HEREDOC_DELIMITER}"
    )

    test_command = " ".join(
        [
            MAP_REPO_VERSION_TO_SPECS[instance["repo"]][instance["version"]][
                "test_cmd"
            ],
            *get_test_directives(instance),
        ]
    )
    eval_commands = [
        "source /opt/miniconda3/bin/activate",
        f"conda activate {env_name}",
        f"cd {repo_directory}",
    ]
    if "eval_commands" in specs:
        eval_commands += specs["eval_commands"]
    eval_commands += [
        f"git config --global --add safe.directory {repo_directory}",  # for nonroot user
        f"cd {repo_directory}",
        # This is just informational, so we have a record
        "git status",
        "git show",
        f"git diff {base_commit}",
        "source /opt/miniconda3/bin/activate",
        f"conda activate {env_name}",
    ]
    if "install" in specs:
        eval_commands.append(specs["install"])
    eval_commands += [
        reset_tests_command,
        fake_apply_test_patch_command,  # If we don't apply some sort of patch the harness won't return the tests which passed
        test_command,
        reset_tests_command,
    ]
    return eval_commands


def rearrange_patches(test_specs):
    """
    rearrange the patches such that slower instance_ids are evaluated first
    this way pipelining will be faster.
    """

    slow_instance_ids = ["sympy__sympy-11870"]

    slow_specs = [
        test_spec
        for test_spec in test_specs
        if test_spec.instance_id in slow_instance_ids
    ]

    if len(slow_specs) != 0:
        print(
            f"rearrange patches such that {[x.instance_id for x in slow_specs]} are evaluated first"
        )
        rearranged_test_specs = slow_specs
        for test_spec in test_specs:
            if test_spec.instance_id not in slow_instance_ids:
                rearranged_test_specs.append(test_spec)
        return rearranged_test_specs
    else:
        return test_specs


def run_reproduction_tests(
    instance_ids: list,
    model_patches: list,
    max_workers: int,
    run_id: str,
    instances_to_run: list,
    timeout: int,
    testing_patches: bool,
    apply_model_patch=True,
    test_jsonl=None,
    dataset_name="princeton-nlp/SWE-bench_Lite",
):
    assert len(instance_ids) == len(
        model_patches
    ), "There must be the same number of instance_ids as model patches"
    resource.setrlimit(resource.RLIMIT_NOFILE, (OPEN_FILE_LIMIT, OPEN_FILE_LIMIT))

    instance_to_reproduction_code = create_instance_test_dict(test_jsonl)

    print(f"Using run_id: {run_id}")

    split = "test"
    client = docker.from_env()
    force_rebuild = False

    predictions = {}

    for idx, one_instance_id in enumerate(instance_ids):
        if not apply_model_patch:
            patch_to_apply = NOOP_PATCH
        else:
            patch_to_apply = model_patches[idx]
        if testing_patches:
            predictions[one_instance_id] = {
                "model_name_or_path": "test",
                "model_patch": NOOP_PATCH,
                "instance_id": one_instance_id,
            }
            # instance_to_reproduction_code[one_instance_id] = patch_to_apply
        else:
            predictions[one_instance_id] = {
                "model_name_or_path": "test",  # TODO change.
                "model_patch": patch_to_apply,
                "instance_id": one_instance_id,
            }

    instances = get_dataset_from_preds(
        dataset_name, split, instance_ids, predictions, run_id
    )

    if not instances:
        print("No instances to run.")
    else:
        build_env_images(client, instances, force_rebuild, max_workers)

    no_f2p_instances = []

    for instance in instances:
        revised_instance = instance
        revised_instance["FAIL_TO_PASS"] = "[]"
        revised_instance["PASS_TO_PASS"] = "[]"

        if instance["instance_id"] in instance_to_reproduction_code:
            revised_instance["production_test"] = instance_to_reproduction_code[
                instance["instance_id"]
            ]
            # only run if there is production test
            no_f2p_instances.append(revised_instance)

    test_specs = list(map(make_reproduction_sec, no_f2p_instances))

    test_specs = rearrange_patches(test_specs)

    instance_image_ids = {x.instance_image_key for x in test_specs}
    existing_images = {
        tag
        for i in client.images.list(all=True)
        for tag in i.tags
        if tag in instance_image_ids
    }
    print(f"Found {len(existing_images)} existing instance images. Will reuse them.")

    # Load in previously evaluated results
    resolved_dict = extract_resolved_info(
        os.path.join("logs", "run_evaluation", run_id, "test")
    )

    if instances_to_run:
        ids = instances_to_run
    else:
        ids = [
            test_spec.instance_id
            for test_spec in test_specs
            if test_spec.instance_id not in list(resolved_dict.keys())
        ]

    results = {}

    print(
        f"Running {len([test_spec for test_spec in test_specs if test_spec.instance_id in ids])} unevaluated instances..."
    )

    # Set the empty instances as not resolving the issue
    for index, patch in enumerate(model_patches):
        if patch == "":
            resolved_dict[instance_ids[index]] = False

    with tqdm(total=len(ids), smoothing=0, colour="MAGENTA") as pbar:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Create a future for running each instance
            futures = {
                executor.submit(
                    run_instance,
                    test_spec,
                    predictions[test_spec.instance_id],
                    False,  # do not remove them.
                    force_rebuild,
                    client,
                    run_id,
                    timeout,
                ): None
                for test_spec in test_specs
                if test_spec.instance_id in ids
            }
            # Wait for each future to complete
            for future in as_completed(futures):
                pbar.update(1)
                result = future.result()
                if result:
                    instance_id = result[0]
                    resolved = result[1][instance_id]["resolved"]
                    resolved_dict[instance_id] = resolved
                    # See if the tests ran successfully
                    if testing_patches:
                        expected_output = "Issue reproduced"
                        other_patterns = ["Issue resolved", "Other issues"]
                    else:
                        expected_output = "Issue resolved"
                        other_patterns = ["Issue reproduced", "Other issues"]
                    path_to_log = f"logs/run_evaluation/{run_id}/{split}/{instance_id}/test_output.txt"
                    passes_tests = txt_file_contains_string(
                        path_to_log, expected_output, other_patterns=other_patterns
                    )
                    results[instance_id] = passes_tests
                try:
                    # Update progress bar, check if instance ran successfully
                    future.result()
                except Exception as e:
                    traceback.print_exc()
                    results[instance_id] = False
                    resolved_dict[instance_id] = False
                    continue

    print("All instances run.")
    return results


def run_tests(
    instance_ids: list,
    model_patches: list,
    max_workers: int,
    run_id: str,
    regression_test_file: str,
    instances_to_run: list,
    timeout: int,
    apply_model_patch=True,
    dataset_name="princeton-nlp/SWE-bench_Lite",
):
    assert len(instance_ids) == len(
        model_patches
    ), "There must be the same number of instance_ids as model patches"
    resource.setrlimit(resource.RLIMIT_NOFILE, (OPEN_FILE_LIMIT, OPEN_FILE_LIMIT))

    print(f"Using run_id: {run_id}")

    split = "test"
    client = docker.from_env()
    force_rebuild = False

    predictions = {}

    for idx, one_instance_id in enumerate(instance_ids):
        if not apply_model_patch:
            patch_to_apply = NOOP_PATCH
        else:
            patch_to_apply = model_patches[idx]
        predictions[one_instance_id] = {
            "model_name_or_path": "test",
            "model_patch": patch_to_apply,
            "instance_id": one_instance_id,
        }

    instances = get_dataset_from_preds(
        dataset_name, split, instance_ids, predictions, run_id
    )

    print(f"Running {len(instances)} unevaluated instances...")
    if not instances:
        print("No instances to run.")
    else:
        build_env_images(client, instances, force_rebuild, max_workers)

    instance_test_dict = {}

    if regression_test_file:
        with open(regression_test_file, "r") as file:
            for line in file:
                json_obj = json.loads(line.strip())
                instance_id = json_obj["instance_id"]
                test = json_obj["tests_passing_in_original_repo"]
                instance_test_dict[instance_id] = test

    no_f2p_instances = []
    for instance in instances:
        revised_instance = instance
        revised_instance["FAIL_TO_PASS"] = "[]"
        # DO NOT USE any of the PASS_TO_PASS in swebench
        # it is either obtained from all passing tests (after LLM filtering)
        # or all tests are ran
        if regression_test_file:
            revised_instance["PASS_TO_PASS"] = instance_test_dict[
                instance["instance_id"]
            ]
        else:
            revised_instance["PASS_TO_PASS"] = "[]"

        no_f2p_instances.append(revised_instance)

    test_specs = list(map(make_regression_spec, no_f2p_instances))

    test_specs = rearrange_patches(test_specs)

    instance_image_ids = {x.instance_image_key for x in test_specs}
    existing_images = {
        tag
        for i in client.images.list(all=True)
        for tag in i.tags
        if tag in instance_image_ids
    }
    print(f"Found {len(existing_images)} existing instance images. Will reuse them.")

    # Load in previously evaluated results
    resolved_dict = extract_resolved_info(
        os.path.join("logs", "run_evaluation", run_id, "test")
    )

    if instances_to_run:
        ids = instances_to_run
    else:
        ids = [
            test_spec.instance_id
            for test_spec in test_specs
            if test_spec.instance_id not in list(resolved_dict.keys())
        ]

    results = {}

    # Set the empty instances as not resolving the issue
    for index, patch in enumerate(model_patches):
        if patch == "":
            resolved_dict[instance_ids[index]] = False

    with tqdm(total=len(ids), smoothing=0, colour="MAGENTA") as pbar:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Create a future for running each instance
            futures = {
                executor.submit(
                    run_instance,
                    test_spec,
                    predictions[test_spec.instance_id],
                    False,  # do not remove them.
                    force_rebuild,
                    client,
                    run_id,
                    timeout,
                ): None
                for test_spec in test_specs
                if test_spec.instance_id in ids
            }
            # Wait for each future to complete
            for future in as_completed(futures):
                pbar.update(1)
                result = future.result()
                if result:
                    instance_id = result[0]
                    resolved = result[1][instance_id]["resolved"]
                    resolved_dict[instance_id] = resolved
                try:
                    # Update progress bar, check if instance ran successfully
                    future.result()
                except Exception as e:
                    traceback.print_exc()
                    results[instance_id] = False  # Or handle the error case as needed
                    resolved_dict[instance_id] = False
                    continue

    print("All instances run.")
    return resolved_dict
