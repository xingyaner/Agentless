import os

def parse_global_stmt_from_code(file_content: str) -> tuple[str, str]:
    """
    【最小化适配版】
    功能：尝试解析 Python 文件的全局变量和导入语句。
    改动点：将导入和解析逻辑包裹在 try-except 中，确保在处理 C/C++ 项目或库缺失时不中断流程。
    """
    try:
        import libcst as cst
        import libcst.matchers as m

        class GlobalVisitor(cst.CSTVisitor):
            def __init__(self):
                self.global_stmt = []
                self.global_imports = []

            def leave_Module(self, original_node: cst.Module):
                for stmt in original_node.body:
                    # 匹配赋值语句
                    if m.matches(stmt, m.SimpleStatementLine()) and m.matches(stmt.body[0], m.Assign()):
                        try:
                            expr = cst.parse_module("").code_for_node(stmt).strip()
                            self.global_stmt.append(expr)
                        except:
                            pass

                    # 匹配导入语句
                    if m.matches(stmt, m.SimpleStatementLine()) and (
                        m.matches(stmt.body[0], m.Import())
                        or m.matches(stmt.body[0], m.ImportFrom())
                    ):
                        try:
                            expr = cst.parse_module("").code_for_node(stmt).strip()
                            self.global_imports.append(expr)
                        except:
                            pass

        tree = cst.parse_module(file_content)
        visitor = GlobalVisitor()
        tree.visit(visitor)

        return "\n".join(visitor.global_stmt), "\n".join(visitor.global_imports)
    except Exception:
        # 非 Python 文件或环境缺失时，返回空字符串，Baseline 将退化为处理全文本内容
        return "", ""

if __name__ == "__main__":
    # 简单的冒烟测试
    sample_code = "import os\nx = 1"
    print(parse_global_stmt_from_code(sample_code))
