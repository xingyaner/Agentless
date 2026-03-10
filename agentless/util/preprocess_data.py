import os
import re

def show_project_structure(structure, spacing=0) -> str:
    """
    将扫描到的物理文件路径转换为格式化的字符串列表，供提示词（Prompt）展示。
    """
    if not isinstance(structure, dict):
        return ""
    # 对路径进行排序，确保模型收到的文件列表顺序一致
    all_paths = sorted(structure.keys())
    return "\n".join(all_paths)

def get_repo_files(structure, filepaths):
    """
    【补全函数】根据路径列表从结构字典中提取文件全文本。
    filepaths: 模型选中的文件路径列表。
    structure: 由 get_repo_structure 生成的 {路径: 行列表} 字典。
    """
    result = {}
    for fp in filepaths:
        if fp in structure:
            # 由于 structure 中存储的是 f.readlines() 的列表，需要 join 还原为字符串
            content = structure[fp]
            if isinstance(content, list):
                result[fp] = "".join(content)
            else:
                result[fp] = str(content)
    return result

def merge_intervals(intervals):
    """
    合并重叠或连续的代码行区间。
    例如: [(1, 10), (5, 15)] -> [(1, 15)]
    """
    if not intervals:
        return []
    intervals.sort()
    merged = [list(intervals[0])]
    for curr in intervals[1:]:
        last = merged[-1]
        if curr[0] <= last[1]:
            last[1] = max(last[1], curr[1])
        else:
            merged.append(list(curr))
    return [tuple(i) for i in merged]

def transfer_arb_locs_to_locs(locs, structure, pred_file, context_window=10, **kwargs):
    """
    【核心工具】将模型定位的“行”或“类”标记转换为物理行号区间。
    """
    line_locs = []
    if isinstance(locs, list):
        for l in locs:
            # 使用正则提取文本中的数字行号，如 "line: 45" 或 "45"
            m = re.search(r"(\d+)", str(l))
            if m:
                line_num = int(m.group(1))
                line_locs.append((line_num, line_num))
    
    # 兜底：如果模型未给出有效行号，默认从第 1 行开始（Baseline 策略）
    if not line_locs:
        line_locs = [(1, 1)]
    
    # 根据 context_window 扩展区间（前后各扩 N 行）
    intervals = [(max(1, s - context_window), e + context_window) for s, e in line_locs]
    return line_locs, merge_intervals(intervals)

def correct_file_paths(model_found_files, files):
    """
    【路径物理锚定工具】
    作用：将模型输出的各种格式路径（带引号、简写、相对路径）映射到磁盘上真实的物理路径。
    """
    all_physical_paths = [f[0] for f in files]
    found = []
    
    if not model_found_files:
        return []

    for mf in model_found_files:
        # 1. 基础清洗
        mf_clean = mf.strip().strip('`').strip("'").strip('"').replace('\\', '/')
        if not mf_clean:
            continue
            
        # 2. 尝试精确匹配
        if mf_clean in all_physical_paths:
            found.append(mf_clean)
            continue
            
        # 3. 尝试模糊后缀匹配（处理 build.sh -> oss-fuzz/projects/hiredis/build.sh）
        matched = False
        for phys_path in all_physical_paths:
            norm_phys = phys_path.replace('\\', '/')
            if norm_phys.endswith(mf_clean) or os.path.basename(norm_phys) == mf_clean:
                found.append(phys_path)
                matched = True
                break
        
        # 4. 尝试反向包含匹配
        if not matched:
            for phys_path in all_physical_paths:
                if phys_path in mf_clean:
                    found.append(phys_path)
                    break
                
    return list(dict.fromkeys(found))

def get_repo_structure(instance_id, repo_name=None, base_commit=None, playground=None):
    """
    【物理扫描版】扫描 OSS-Fuzz 项目配置和第三方软件源码。
    """
    structure = {}
    config_base = os.path.abspath(f"oss-fuzz/projects/{instance_id}")
    source_base = os.path.abspath(f"process/project/{instance_id}")

    target_paths = [
        ("oss-fuzz_config", config_base),
        ("source_code", source_base)
    ]

    for label, base_path in target_paths:
        if not os.path.exists(base_path):
            continue
            
        for root, dirs, files in os.walk(base_path):
            if '.git' in dirs: dirs.remove('.git')
            if '__pycache__' in dirs: dirs.remove('__pycache__')
            
            for file in files:
                if file.endswith(('.o', '.a', '.so', '.pyc', '.bin', '.gz', '.zip')):
                    continue
                
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, os.getcwd())
                
                try:
                    with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
                        structure[rel_path] = f.readlines()
                except Exception:
                    pass
                    
    return structure

def get_full_file_paths_and_classes_and_functions(structure):
    """展平物理扫描结果，适配索引系统"""
    files = []
    if not isinstance(structure, dict):
        return files, [], []
    for path, content in structure.items():
        files.append((path, content))
    return files, [], []

def line_wrap_content(content, context_intervals=None, no_line_number=False, **kwargs):
    """为代码添加行号，便于模型定位"""
    lines = content.split("\n")
    if not context_intervals:
        context_intervals = [(1, len(lines))]
    
    new_lines = []
    for start, end in context_intervals:
        for i in range(max(0, start-1), min(len(lines), end)):
            prefix = f"{i+1}|" if not no_line_number else ""
            new_lines.append(f"{prefix}{lines[i]}")
    return "\n".join(new_lines)

# 保留占位符
def check_contains_valid_loc(file_to_locs, structure): return len(file_to_locs) > 0
def filter_none_python(s): pass
def filter_out_test_files(s): pass
