import argparse
import concurrent.futures
import json
import os
from difflib import unified_diff
from threading import Lock
from tqdm import tqdm
import re

from agentless.util.api_requests import num_tokens_from_messages
from agentless.util.model import make_model
from agentless.util.postprocess_data import (
    check_code_differ_by_just_empty_lines,
    check_syntax,
    extract_python_blocks,
    fake_git_repo,
    lint_code,
    parse_diff_edit_commands,
    parse_edit_commands,
    parse_str_replace_edit_commands,
    split_edit_multifile_commands,
)
from agentless.util.preprocess_data import (
    get_full_file_paths_and_classes_and_functions,
    get_repo_structure,
    line_wrap_content,
    transfer_arb_locs_to_locs,
)
from agentless.util.utils import cleanup_logger, load_jsonl, setup_logger

repair_relevant_file_instruction = """Below are code segments from relevant files. One or more of these files contain the cause of the build failure."""
repair_prompt_combine_topn_cot_diff = """Solving build error based on this log:
{problem_statement}

--- BEGIN FILE CONTEXT ---
{content}
--- END FILE CONTEXT ---

Please analyze the error and generate *SEARCH/REPLACE* edits to fix it. 
You MUST wrap the edit blocks in ```python ... ```.

Format:
```python
### path/to/file
<<<<<<< SEARCH
[exact lines from original file]
=======
[modified lines]
>>>>>>> REPLACE
"""


def get_preceding_filepath(full_text: str, patch_start_index: int, known_physical_paths: list) -> str:
    """
    【强精准路径定位】
    1. 扫描 full_text 中所有的 "### path/to/file" 标记。
    2. 找到在 patch_start_index 之前且距离最近的一个标记。
    3. 将提取的路径与物理磁盘上的已知路径进行映射。
    """
    # 匹配 ### 路径 或直接匹配常见文件名格式
    # 允许模型输出 ### /src/hiredis/build.sh 或 ### build.sh
    anchors = []
    for match in re.finditer(r"###\s*([^\n\s]+)", full_text):
        anchors.append({
            "offset": match.start(),
            "path": match.group(1).strip().strip("'").strip('"')
        })

    if not anchors:
        return None

    # 寻找小于 patch_start_index 的最大 offset
    target_anchor = None
    for anchor in anchors:
        if anchor["offset"] < patch_start_index:
            target_anchor = anchor
        else:
            break

    if not target_anchor:
        return None

    potential_path = target_anchor["path"]
    
    # 执行物理路径映射 (解决 R1 路径简写问题)
    # 优先精确匹配
    if potential_path in known_physical_paths:
        return potential_path
    
    # 后缀模糊匹配 (处理 build.sh -> /root/oss-fuzz/projects/hiredis/build.sh)
    for phys_path in known_physical_paths:
        if phys_path.replace('\\', '/').endswith(potential_path.replace('\\', '/')):
            return phys_path

    return None

def _post_process_multifile_repair(
    raw_output: str,
    file_contents: dict[str, str],
    logger,
    file_loc_intervals: dict[str, list],
    diff_format=False,
    str_replace_format=False,
) -> tuple[list[str], list[str]]:
    """【修正版】移除 eval() 调用，解决 NameError"""
    edit_multifile_commands = extract_python_blocks(raw_output)
    edited_files = []
    new_contents = []
    try:
        file_to_commands = split_edit_multifile_commands(
            edit_multifile_commands,
            diff_format=diff_format,
            str_replace_format=str_replace_format,
        )
    except Exception as e:
        logger.error(f"Split commands error: {e}")
        return edited_files, new_contents

    for edited_file_key, edit_commands in file_to_commands.items():
        # 【核心修复】安全处理路径名，不再执行 eval()
        edited_file_path = edited_file_key.strip().strip("'").strip('"')
        
        # 后缀路径匹配逻辑：将模型猜测的路径映射到物理路径
        matched_physical_file = None
        if edited_file_path in file_contents:
            matched_physical_file = edited_file_path
        else:
            for phys_path in file_contents.keys():
                if phys_path.endswith(edited_file_path):
                    matched_physical_file = phys_path
                    break
        
        if not matched_physical_file:
            continue

        try:
            content = file_contents[matched_physical_file]
            new_content = parse_diff_edit_commands(edit_commands, content)
            
            if matched_physical_file and new_content:
                edited_files.append(matched_physical_file)
                new_contents.append(new_content)
        except Exception as e:
            logger.error(f"Parse error for {matched_physical_file}: {e}")

    return edited_files, new_contents

def construct_topn_file_context(file_to_locs, pred_files, file_contents, structure, context_window: int, **kwargs):
    topn_content = ""
    file_loc_intervals = {}
    for pred_file in pred_files:
        if pred_file in file_contents:
            content = file_contents[pred_file]
            # Baseline 默认展示全文件
            topn_content += f"### {pred_file}\n{content}\n\n"
            file_loc_intervals[pred_file] = [(1, len(content.splitlines()))]
    return topn_content, file_loc_intervals


def process_loc_oss_fuzz(loc, args, log_content, project_info, logger):
    """
    【最终鲁棒集成版】
    1. 包含 3 次路径纠偏重试逻辑，解决 LLM 简写路径导致的 Context Empty。
    2. 注入 [PATH_ID] 与 STRICT COMPLIANCE 约束，强化 R1 指令遵循。
    3. 利用滑动窗口物理对齐，从磁盘拉取真实原文（彻底解决缩进陷阱）。
    """
    from agentless.util.preprocess_data import get_full_file_paths_and_classes_and_functions
    from agentless.util.postprocess_data import robust_sliding_window_match, map_to_physical_path, get_closest_paths
    from agentless.util.model import make_model
    import re
    import os

    instance_id = loc["instance_id"]
    initial_pred_files = loc["found_files"]  # 包含强制注入的配置文件路径
    structure = loc["structure"]

    # 准备物理路径库 (Key 统一为项目相对路径)
    files_info, _, _ = get_full_file_paths_and_classes_and_functions(structure)
    physical_paths = sorted([f[0] for f in files_info])
    all_physical_contents = {f[0]: "".join(f[1]) for f in files_info}

    # =================================================================
    # --- 阶段 1: 路径有效性验证与反馈重试 (最多 3 次) ---
    # =================================================================

    retry_count = 0
    max_retries = 3
    current_pred_files = initial_pred_files
    final_file_contents = {}

    while retry_count < max_retries:
        valid_mapped = []
        invalid_found = []

        for pf in current_pred_files:
            # 执行 路径对齐：匹配逻辑（精确 -> 后缀）
            mapped = map_to_physical_path(pf, physical_paths)
            if mapped:
                valid_mapped.append(mapped)
                final_file_contents[mapped] = all_physical_contents[mapped]
            else:
                invalid_found.append(pf)

        # 检查是否成功获取内容：如果所有路径都对齐了，或者至少配置路径对齐了
        if not invalid_found and final_file_contents:
            break

        retry_count += 1
        logger.warning(f"--- [Path Alignment Retry {retry_count}] Model provided invalid paths: {invalid_found} ---")

        # 构造反馈给模型的知识注入信息
        feedback_msg = (
            f"ERROR: The following file paths you provided do not exist in the project structure:\n"
        )
        for ip in invalid_found:
            suggestions = get_closest_paths(ip, physical_paths)
            feedback_msg += f"- '{ip}' (NOT FOUND). "
            if suggestions:
                feedback_msg += f"Did you mean: {', '.join(suggestions)}?\n"
            else:
                feedback_msg += "Please refer to the Repository Structure and use the FULL path.\n"

        feedback_msg += (
            "\nINSTRUCTION: Provide the CORRECT, COMPREHENSIVE paths for the files you need.\n"
            "Example: 'oss-fuzz/projects/hiredis/build.sh' (NOT just 'build.sh')\n"
            "Reply with paths wrapped in ```."
        )

        # 快速咨询 R1 获取修正路径
        retry_model = make_model(model=args.model, logger=logger, backend=args.backend, max_tokens=2048)
        retry_res = retry_model.codegen(feedback_msg, num_samples=1)[0]

        # 重新解析路径
        from agentless.fl.localize import LLMFL
        temp_fl = LLMFL(instance_id, {}, "", args.model, args.backend, logger)
        current_pred_files = temp_fl._parse_model_return_lines(retry_res["response"])

    if not final_file_contents:
        logger.error("--- [Critical Failure] No valid paths identified after 3 retries. ---")
        return ""

    # =================================================================
    # --- 阶段 2: 构建增强版修复提示词 ---
    # =================================================================

    file_context_str = ""
    for f_path, content in final_file_contents.items():
        # 获取物理路径在总表中的索引作为 PATH_ID
        pid = physical_paths.index(f_path)
        file_context_str += f"### [PATH_ID_{pid}]: {f_path} ###\n{content}\n\n"

    from agentless.repair.repair import repair_prompt_combine_topn_cot_diff

    # 注入强制指令，要求模型执行复制粘贴
    strict_instruction = (
        "CRITICAL RULES FOR PATCHING:\n"
        "1. You MUST COPY-PASTE the original lines VERBATIM from the headers provided below into your SEARCH block.\n"
        "2. You MUST use the COMPREHENSIVE path from the headers (e.g., '### oss-fuzz/projects/hiredis/build.sh').\n"
        "3. Any deviation in flags or characters in the SEARCH block will cause the repair to fail.\n"
    )

    prompt = strict_instruction + repair_prompt_combine_topn_cot_diff.format(
        problem_statement=log_content,
        content=file_context_str.rstrip()
    )

    # =================================================================
    # --- 阶段 3: 执行生成与物理原文对齐 ---
    # =================================================================

    # 正式开始 Repair (DeepSeek R1)
    model_obj = make_model(model=args.model, logger=logger, backend=args.backend, max_tokens=16384)
    trajs = model_obj.codegen(prompt, num_samples=args.max_samples)

    for traj in trajs:
        raw_output = traj["response"]
        chunks = raw_output.split("<<<<<<< SEARCH")
        formatted_patch = ""

        for i in range(1, len(chunks)):
            # 向上寻找路径标记 (###)
            prev_context = chunks[i - 1][-400:]
            current_chunk = chunks[i]
            if "=======" not in current_chunk or ">>>>>>> REPLACE" not in current_chunk: continue

            target_phys_f = None
            # 强化路径识别正则
            path_match = re.search(r"###\s*([^\n\s#]+)", prev_context)
            if path_match:
                guessed_p = path_match.group(1).strip().strip("'\"`").replace('\\', '/')
                # 使用后缀匹配对齐物理路径
                for pp in final_file_contents.keys():
                    if pp.lower().endswith(guessed_p.lower()) or guessed_p.lower() in pp.lower():
                        target_phys_f = pp
                        break

            if not target_phys_f: continue

            try:
                parts = current_chunk.split("=======")
                search_block = parts[0].strip("\n\r")
                replace_block = parts[1].split(">>>>>>> REPLACE")[0].strip("\n\r")

                full_content = final_file_contents[target_phys_f]
                # 调用我们在 postprocess_data 里的模糊匹配器
                match_res = robust_sliding_window_match(search_block, full_content)

                if match_res:
                    start_idx, end_idx = match_res
                    phys_lines = full_content.splitlines()
                    # 【核心亮点】：从物理磁盘拉取真实的原始行，确保补丁 100% 匹配
                    actual_orig_text = "\n".join(phys_lines[start_idx: end_idx])

                    formatted_patch += f"---=== FILE ===---\n{target_phys_f}\n"
                    formatted_patch += f"---=== ORIGINAL ===---\n{actual_orig_text}\n"
                    formatted_patch += f"---=== REPLACEMENT ===---\n{replace_block}\n"
                    logger.info(f"--- [Match Success] Verified location in {target_phys_f} at line {start_idx + 1} ---")
                else:
                    logger.warning(f"--- [Match Fail] SEARCH block not found in physical file: {target_phys_f} ---")
            except Exception as e:
                logger.error(f"Error parsing chunk: {e}")

        if formatted_patch.strip():
            return formatted_patch

    return ""


def main():
    pass # 保持原文件 main 定义占位

if __name__ == "__main__":
    main()
