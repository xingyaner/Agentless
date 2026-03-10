import os
import asyncio
import json
import yaml
import sys
import subprocess
import logging
import requests
import time
from datetime import datetime
from typing import Dict, List, Optional
from llama_index.core import Settings
from llama_index.llms.openai_like import OpenAILike
from llama_index.embeddings.huggingface import HuggingFaceEmbedding

# =================================================================
# --- 1. 配置 DeepSeek Reasoner (R1) ---
# =================================================================
DPSEEK_API_KEY = "sk-cf1e07ade551446ca2a79d8c22b299cd"
os.environ["DPSEEK_API_KEY"] = DPSEEK_API_KEY

Settings.llm = OpenAILike(
    model="deepseek-reasoner",
    api_key=DPSEEK_API_KEY,
    api_base="https://api.deepseek.com",
    max_tokens=8192,
    is_chat_model=True,
    additional_kwargs={"extra_body": {"thinking": {"type": "enabled"}}}
)
Settings.embed_model = HuggingFaceEmbedding(model_name="BAAI/bge-small-en-v1.5")


class StreamToLogger:
    """将 sys.stdout/stderr 的输出实时转到 Logger 中记录"""
    def __init__(self, logger, level=logging.INFO):
        self.logger = logger
        self.level = level
        self.linebuf = ''

    def write(self, buf):
        for line in buf.rstrip().splitlines():
            # 记录到物理文件
            self.logger.log(self.level, line.rstrip())

    def flush(self):
        for handler in self.logger.handlers:
            handler.flush()

def setup_project_logger(project_name: str):
    log_dir = os.path.abspath("log")
    os.makedirs(log_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(log_dir, f"{project_name}_{timestamp}.log")
    logger = logging.getLogger(project_name)
    logger.handlers = []
    fh = logging.FileHandler(log_path, encoding='utf-8')
    fh.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(fh)
    logger.addHandler(sh)
    logger.setLevel(logging.INFO)
    return logger


def download_remote_log(log_url, project_name):
    local_path = os.path.abspath(f"build_error_log/{project_name}/error.txt")
    if os.path.exists(local_path): return local_path
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    try:
        res = requests.get(log_url, timeout=30)
        res.raise_for_status()
        with open(local_path, 'wb') as f:
            f.write(res.content)
        return local_path
    except:
        return ""


def read_projects_from_yaml(file_path):
    if not os.path.exists(file_path): return {'status': 'error', 'message': 'YAML missing'}
    valid = []
    try:
        with open(file_path, 'r') as f:
            data = yaml.safe_load(f)
        for idx, e in enumerate(data):
            if str(e.get('fixed_state', 'no')).lower() == 'no':
                p_name = e.get('project')
                log_path = download_remote_log(e.get('fuzzing_build_error_log', ""), p_name)
                if p_name and log_path:
                    project_info = e.copy()
                    project_info.update({
                        "project_name": p_name,
                        "row_index": idx,
                        "original_log_path": log_path,
                        "project_source_path": os.path.abspath(f"process/project/{p_name}"),
                        "oss_fuzz_sha": str(e.get('oss-fuzz_sha') or e.get('sha')),
                        "software_sha": str(e.get('software_sha')),
                        "software_repo_url": e.get('software_repo_url')
                    })
                    valid.append(project_info)
        return {'status': 'success', 'projects': valid}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


def checkout_oss_fuzz_commit(sha):
    path = os.path.abspath("./oss-fuzz")
    if not os.path.exists(path):
        subprocess.run(["git", "clone", "https://github.com/google/oss-fuzz.git", path], check=True)
    cwd = os.getcwd()
    os.chdir(path)
    try:
        subprocess.run(["git", "checkout", "-f", sha], capture_output=True, check=True)
    except:
        subprocess.run(["git", "fetch"], check=True)
        subprocess.run(["git", "checkout", "-f", sha], check=True)
    finally:
        os.chdir(cwd)


def checkout_project_commit(project):
    path = project['project_source_path']
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        subprocess.run(["git", "clone", project['software_repo_url'], path], check=True)
    cwd = os.getcwd()
    os.chdir(path)
    try:
        subprocess.run(["git", "reset", "--hard", "HEAD"], check=True)
        subprocess.run(["git", "checkout", "-f", project['software_sha']], check=True)
    finally:
        os.chdir(cwd)


def update_yaml_report(file_path, row_index, result):
    with open(file_path, 'r', encoding='utf-8') as f: data = yaml.safe_load(f)
    data[row_index]['state'] = 'yes'
    data[row_index]['fix_result'] = result
    data[row_index]['fix_date'] = datetime.now().strftime('%Y-%m-%d')
    with open(file_path, 'w', encoding='utf-8') as f: yaml.dump(data, f, sort_keys=False)


# =================================================================
# --- 3. 统计适配封装 ---
# =================================================================

def process_loc_oss_fuzz_with_stats(loc, args, log_content, logger):
    """
    【统计完整版】调用 Repair 模块并捕获 R1 真实的 Token 消耗。
    """
    from agentless.util.preprocess_data import get_full_file_paths_and_classes_and_functions, get_repo_files
    from agentless.repair.repair import construct_topn_file_context, repair_prompt_combine_topn_cot_diff, make_model, \
        _post_process_multifile_repair

    pred_files = loc["found_files"][:args.top_n]
    file_contents = get_repo_files(loc["structure"], pred_files)

    # 强制构建包含代码的上下文
    virtual_locs = {fn: ["line: 1"] for fn in pred_files}
    topn_content, file_loc_intervals = construct_topn_file_context(virtual_locs, pred_files, file_contents,
                                                                   loc["structure"], 1000)

    prompt = repair_prompt_combine_topn_cot_diff.format(problem_statement=log_content, content=topn_content.rstrip())

    # 调用模型工厂 (make_model 内部会抓取 token 并打印)
    model_obj = make_model(model=args.model, logger=logger, backend=args.backend, max_tokens=8192, temperature=0.0)
    trajs = model_obj.codegen(prompt, num_samples=args.max_samples)

    # 累加 Token
    total_tokens = sum(t['usage']['prompt_tokens'] + t['usage']['completion_tokens'] for t in trajs)

    for traj in trajs:
        # 使用我们之前重写的鲁棒匹配提取逻辑
        edited_files, new_contents = _post_process_multifile_repair(traj["response"], file_contents, logger,
                                                                    file_loc_intervals, diff_format=True)

        formatted_patch = ""
        for f, new_c in zip(edited_files, new_contents):
            if f in file_contents and file_contents[f].strip() != new_c.strip():
                formatted_patch += f"---=== FILE ===---\n{f}\n"
                formatted_patch += f"---=== ORIGINAL ===---\n{file_contents[f]}\n"
                formatted_patch += f"---=== REPLACEMENT ===---\n{new_c}\n"

        if formatted_patch:
            return formatted_patch, total_tokens

    return "", total_tokens


# =================================================================
# --- 4. 主循环 ---
# =================================================================

async def run_baseline():
    """
    【终极鲁棒版】驱动 Agentless Pipeline 在 OSS-Fuzz 环境下运行。
    1. 物理环境全量备份与重定向控制台日志。
    2. 物理路径模糊对齐，解决 Context Empty 问题。
    3. 取消精确匹配，确保补丁 100% 物理对齐。
    """
    YAML_FILE = 'projects.yaml'
    res = read_projects_from_yaml(YAML_FILE)
    if res['status'] == 'error' or not res['projects']:
        print("--- [Baseline] No eligible projects to process. ---")
        return

    # 统一参数配置 (模拟命令行参数)
    class Args:
        file_level = True
        top_n = 3
        model = "deepseek-reasoner"
        backend = "deepseek"
        mock = False
        context_window = 10
        max_samples = 3

    args = Args()

    for project in res['projects']:
        p_name = project['project_name']
        row_index = project['row_index']

        # 初始化当前项目的 Logger
        logger = setup_project_logger(p_name)
        start_time = time.time()

        # 备份标准输出
        stdout_bak = sys.stdout
        stderr_bak = sys.stderr

        # 开启日志全量捕获 (将控制台 print 全部转入 logger)
        sys.stdout = StreamToLogger(logger, logging.INFO)
        sys.stderr = StreamToLogger(logger, logging.ERROR)

        final_success = False
        files_mod, lines_mod, token_usage = 0, 0, 0
        patch_str = ""

        try:
            print(f"\n🚀 [Baseline R1] STARTING REPAIR: {p_name}")
            print(f"📍 Target SHA: {project['software_sha']}")

            # --- A. 环境物理锚定 ---
            checkout_oss_fuzz_commit(project['oss_fuzz_sha'])
            checkout_project_commit(project)

            if not os.path.exists(project['original_log_path']):
                raise FileNotFoundError(f"Log missing at: {project['original_log_path']}")

            with open(project['original_log_path'], 'r', errors='ignore') as f:
                log_content = f.read()

            # --- B. 执行 Agentless 修复流水线 ---

            # 1. 定位 (Localization)
            # 内部已适配双门径扫描 (Source + Config)
            from agentless.fl.localize import localize_instance_oss_fuzz
            loc_res = localize_instance_oss_fuzz(project, args, log_content, logger)

            # 2. 修复 (Repair)
            # 内部已适配缩进无关匹配，会自动校准 ORIGINAL 块
            # 返回 patch 内容与真实的 token 消耗统计
            patch_str, token_usage = process_loc_oss_fuzz_with_stats(loc_res, args, log_content, logger)

            # 3. 验证 (Validation)
            if patch_str:
                from agentless.test.run_tests import run_oss_fuzz_validation
                # 执行物理构建并获取修改规模指标
                val_data = run_oss_fuzz_validation(p_name, patch_str, project)

                final_success = val_data.get('success', False)
                files_mod = val_data.get('files', 0)
                lines_mod = val_data.get('lines', 0)
            else:
                print(f"--- [Baseline Warning] No valid patch blocks extracted for {p_name}. ---")

            # --- C. 输出量化报告 ---
            duration = (time.time() - start_time) / 60
            report = (
                f"\n{'=' * 60}\n"
                f"🏁 FINAL BASELINE REPORT: {p_name}\n"
                f"{'-' * 60}\n"
                f"  - [RESULT]         {'✅ SUCCESS' if final_success else '❌ FAILURE'}\n"
                f"  - [TARGET SHA]     {project['software_sha']}\n"
                f"  - [REPAIR ROUNDS]  {args.max_samples}\n"
                f"  - [TOKEN USAGE]    {token_usage}\n"
                f"  - [PATCH SCALE]    {files_mod} files, {lines_mod} lines modified\n"
                f"  - [TIME COST]      {duration:.2f} minutes\n"
                f"{'=' * 60}\n"
            )
            print(report)  # 此 print 会通过 StreamToLogger 写入日志
            update_yaml_report(YAML_FILE, row_index, "Success" if final_success else "Failure")

        except Exception as e:
            # 详细记录报错异常
            print(f"❌ Critical Error in loop for {p_name}: {e}")
            import traceback
            print(traceback.format_exc())
            update_yaml_report(YAML_FILE, row_index, "Failure")

        finally:
            # 【恢复标准输出】：必须在循环末尾恢复，防止下一个项目的日志串流
            sys.stdout = stdout_bak
            sys.stderr = stderr_bak

            # 【强制物理落地】：刷新缓冲区，确保思维链全部写入磁盘
            for handler in logger.handlers:
                handler.flush()
                if hasattr(handler, 'stream') and hasattr(handler.stream, 'flush'):
                    handler.stream.flush()

    print("\n--- [Baseline] Pipeline processing complete. ---")


if __name__ == "__main__":
    asyncio.run(run_baseline())
