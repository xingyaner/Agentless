#!/bin/bash
# 文件名: remove_last_three.sh
# 功能: 删除 projects.yaml 中每个项目条目的最后三行元数据
# 用法: ./remove_last_three.sh   (需与 projects.yaml 同目录)

find . -name "__pycache__" -type d -exec rm -rf {} +
rm -rf Agentless/agentless/fl/__pycache__
rm -rf Agentless/agentless/util/__pycache__
file="projects.yaml"
backup="${file}.bak"

# 检查文件是否存在
if [ ! -f "$file" ]; then
    echo "错误: $file 不存在"
    exit 1
fi

# 创建备份
cp "$file" "$backup"

# 使用 awk 处理文件：保留每个条目前面的所有行，去掉最后三行
awk '
BEGIN { entry = "" }
/^- project:/ {
    if (entry != "") {
        n = split(entry, lines, "\n")
        for (i = 1; i <= n - 3; i++) {
            print lines[i]
        }
    }
    entry = $0
    next
}
{
    if (entry != "") {
        entry = entry "\n" $0
    } else {
        # 如果文件开头有非条目内容（如注释），直接保留
        print $0
    }
}
END {
    if (entry != "") {
        n = split(entry, lines, "\n")
        for (i = 1; i <= n - 3; i++) {
            print lines[i]
        }
    }
}' "$backup" > "$file"

echo "处理完成！原文件已备份为 $backup"
