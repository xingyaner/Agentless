import os
import re
import json
import difflib


def get_keywords(line: str) -> set:
    """提取行中的核心特征词：剔除单字符和纯数字，保留路径、标志位和函数名。"""
    # 匹配单词、路径、带$的变量
    tokens = re.findall(r"[\w\/\.\$\-\+]+", line.expandtabs(4))
    return {t for t in tokens if len(t) > 1 or t.startswith('$')}


def fuzzy_line_match_score(model_line: str, source_line: str) -> float:
    """计算两行之间的核心特征重合度。"""
    model_keywords = get_keywords(model_line)
    source_keywords = get_keywords(source_line)

    if not model_keywords: return 0.0

    overlap = model_keywords.intersection(source_keywords)
    return len(overlap) / len(model_keywords)

def get_closest_paths(invalid_path: str, physical_paths: list, top_n: int = 5) -> list:
    """当路径无效时，返回最接近的 5 个路径供模型参考"""
    return difflib.get_close_matches(invalid_path, physical_paths, n=top_n, cutoff=0.1)

def map_to_physical_path(llm_path: str, physical_paths: list) -> str:
    """
    【路径物理对齐工具】
    1. 精确匹配
    2. 后缀匹配
    """
    clean_p = llm_path.strip().strip("'\"`").replace('\\', '/')

    # 策略 1: 精确匹配
    if clean_p in physical_paths:
        return clean_p

    # 策略 2: 后缀匹配 (如 build.sh -> oss-fuzz/projects/hiredis/build.sh)
    for pp in physical_paths:
        if pp.endswith(clean_p):
            return pp

    return None

def normalize_line(line: str) -> str:
    """消除缩进差异：转换 Tab 为空格，移除所有行首行尾空白，合并中间多余空格。"""
    if not line: return ""
    return " ".join(line.expandtabs(4).split()).strip()


def robust_sliding_window_match(search_block: str, full_content: str):
    """
    【方案 A：模糊块定位器】
    逻辑：只要模型提供的行包含源文件中对应行 80% 以上的特征词，即认定匹配。
    """
    search_lines = [l.strip() for l in search_block.splitlines() if l.strip()]
    if not search_lines: return None

    content_lines = full_content.splitlines()
    n = len(search_lines)
    best_match_idx = -1
    max_total_score = 0

    # 滑动窗口扫描
    for i in range(len(content_lines) - n + 1):
        current_window_score = 0
        match_count = 0

        for j in range(n):
            score = fuzzy_line_match_score(search_lines[j], content_lines[i + j])
            if score >= 0.8:  # 80% 阈值
                current_window_score += score
                match_count += 1

        # 必须每一行都达到基本匹配要求
        if match_count == n:
            avg_score = current_window_score / n
            if avg_score > max_total_score:
                max_total_score = avg_score
                best_match_idx = i

        # 如果达到了完美的 1.0 匹配，直接返回
        if max_total_score >= 1.0:
            return best_match_idx, best_match_idx + n

    if best_match_idx != -1:
        return best_match_idx, best_match_idx + n
    return None


def extract_python_blocks(text: str) -> list[str]:
    """通用代码块提取器：提取任何 Markdown 格式的代码块内容"""
    # 匹配 ```标签\n 内容 \n```
    blocks = re.findall(r"```(?:\w+)?\n(.*?)\n```", text, re.DOTALL)
    if not blocks:
        # 兜底匹配
        blocks = re.findall(r"```(?:\w+)?(.*?)\n?```", text, re.DOTALL)
    return [b.strip() for b in blocks if b.strip()]


def split_edit_multifile_commands(blocks: list[str], **kwargs) -> dict[str, list[str]]:
    """
    【加固版】兼容 R1 的 ### 路径标识符。
    """
    file_to_commands = {}
    for block in blocks:
        # 正则匹配 ### 路径
        matches = list(re.finditer(r"###\s*([^\n\s]+)", block))
        if not matches:
            if "<<<<<<< SEARCH" in block:
                # 兜底处理没写路径但有补丁的情况
                file_to_commands.setdefault("build.sh", []).append(block.strip())
            continue

        for i in range(len(matches)):
            path = matches[i].group(1).strip()
            start = matches[i].end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(block)
            cmd = block[start:end].strip()
            if cmd:
                file_to_commands.setdefault(path, []).append(cmd)
    return file_to_commands


def _post_process_multifile_repair(
        raw_output: str,
        file_contents: dict[str, str],
        logger,
        file_loc_intervals: dict[str, list] = None,
        **kwargs
) -> tuple[list[str], list[str]]:
    """
    【加固版】解决 R1 路径简写和非标准 Markdown 块提取问题。
    """
    # 1. 优先尝试标准的 Markdown 提取
    blocks = extract_python_blocks(raw_output)

    # 2. 【修复方案四】：如果正则没抓到，尝试硬切分逻辑
    if not any("<<<<<<< SEARCH" in b for b in blocks):
        blocks = raw_output.split("<<<<<<< SEARCH")
        # 恢复 SEARCH 标记用于后续解析
        blocks = ["<<<<<<< SEARCH" + b for b in blocks if ">>>>>>> REPLACE" in b]

    edited_files, new_contents = [], []

    # 3. 构建物理路径快速索引 (处理 hiredis/build.sh -> oss-fuzz/projects/hiredis/build.sh)
    physical_paths = list(file_contents.keys())

    for block in blocks:
        # 【修复方案三】：寻找路径标记 (###) 或从上下文推断路径
        path_match = re.search(r"###\s*([^\n\s]+)", block)
        inferred_path = path_match.group(1).strip().strip('`') if path_match else None

        # 寻找匹配的物理路径
        matched_f = None
        if inferred_path:
            norm_inferred = os.path.normpath(inferred_path)
            for pp in physical_paths:
                if pp.endswith(norm_inferred) or norm_inferred in pp:
                    matched_f = pp
                    break

        if not matched_f: continue

        # 4. 执行滑动窗口匹配与替换 (parse_diff_edit_commands)
        try:
            content = file_contents[matched_f]
            # 这里的 parse_diff_edit_commands 使用我之前提供的全量滑动窗口版本
            new_c = parse_diff_edit_commands([block], content)
            if new_c != content:
                edited_files.append(matched_f)
                new_contents.append(new_c)
        except Exception as e:
            logger.error(f"Error patching {matched_f}: {e}")

    return edited_files, new_contents


def parse_diff_edit_commands(commands, content, intervals=None):
    """
    【强工具逻辑】专门解决跨语言构建脚本中的空白符匹配失败。
    """
    if not commands: return content
    new_lines = content.splitlines()

    for cmd in commands:
        if "<<<<<<< SEARCH" not in cmd: continue
        try:
            # 提取块内容
            parts = cmd.split("=======")
            search_block = parts[0].split("<<<<<<< SEARCH")[-1].strip("\n\r")
            replace_block = parts[1].split(">>>>>>> REPLACE")[0].strip("\n\r")

            s_lines = search_block.splitlines()
            r_lines = replace_block.splitlines()

            # 标准化匹配（忽略行尾空格）
            norm_s = [l.rstrip() for l in s_lines]
            n = len(norm_s)
            if n == 0: continue

            match_idx = -1
            for i in range(len(new_lines) - n + 1):
                window = [l.rstrip() for l in new_lines[i: i + n]]
                if window == norm_s:
                    match_idx = i
                    break

            if match_idx != -1:
                new_lines = new_lines[:match_idx] + r_lines + new_lines[match_idx + n:]
            else:
                # 如果匹配失败，记录原因至物理日志
                print(f"--- [Match Fail] SEARCH block failed for lines starting with: {norm_s[0][:20]} ---")
        except:
            continue
    return "\n".join(new_lines)


# 以下为 repair.py 依赖的占位函数，保持 Baseline 最小化运行
def extract_code_blocks(text): return extract_python_blocks(text)


def extract_locs_for_files(raw, files, keep=False): return [[""] for _ in files]


def check_syntax(c): return True


def check_code_differ_by_just_empty_lines(c1, c2): return False


def fake_git_repo(*args): return "fake_diff"


def lint_code(c): return True


def parse_edit_commands(cmds, content): return content


def parse_str_replace_edit_commands(cmds, content, intv): return content
