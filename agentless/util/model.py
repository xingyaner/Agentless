import os
import json
import logging
from abc import ABC, abstractmethod
from typing import List
from llama_index.llms.openai_like import OpenAILike

# =================================================================
# --- 1. 基础类定义 (保持 Agentless 架构) ---
# =================================================================

class DecoderBase(ABC):
    def __init__(
        self,
        name: str,
        logger,
        batch_size: int = 1,
        temperature: float = 0.0,
        max_new_tokens: int = 8192,
    ) -> None:
        self.name = name
        self.logger = logger
        self.batch_size = batch_size
        self.temperature = temperature
        self.max_new_tokens = max_new_tokens

    @abstractmethod
    def codegen(self, prompt: str, num_samples: int = 1, **kwargs) -> List[dict]:
        results = []
        for i in range(num_samples):
            response = self.llm.complete(prompt)

            # 1. 抓取思维链
            reasoning = "No reasoning content found."
            if hasattr(response, 'raw') and response.raw:
                try:
                    raw_msg = response.raw.choices[0].message
                    reasoning = getattr(raw_msg, 'reasoning_content', "Reasoning not found")
                except:
                    pass

            # 2. 【全量记录】：将控制台信息同步至 Logger
            log_msg = f"\n{'=' * 20} DeepSeek R1 Generation (Sample {i + 1}) {'=' * 20}\n"
            log_msg += f"--- 🤔 REASONING ---\n{reasoning}\n"
            log_msg += f"--- 📝 FINAL CONTENT ---\n{response.text}\n"
            log_msg += f"{'=' * 60}\n"

            self.logger.info(log_msg)  # 写入物理文件
            print(log_msg)  # 输出到终端

            results.append({
                "response": response.text,
                "usage": getattr(response.raw, 'usage', {"prompt_tokens": 0, "completion_tokens": 0})
            })
        return results

    def __repr__(self) -> str:
        return self.name

# =================================================================
# --- 2. 核心适配类：DeepSeek Reasoner (R1) 包装器 ---
# =================================================================

class DeepSeekR1Wrapper(DecoderBase):
    """
    【最终稳定版】适配 DeepSeek R1 思考模式，解决 Pydantic 访问冲突，支持指标统计。
    """
    def __init__(self, api_key, api_base, max_tokens=8192, **kwargs):
        # 移除 LlamaIndex 可能冲突的参数
        name = kwargs.pop("model", "deepseek-reasoner")
        logger = kwargs.pop("logger", logging.getLogger("model"))
        temp = kwargs.pop("temperature", 0.0)
        
        super().__init__(name, logger, temperature=temp, max_new_tokens=max_tokens)
        
        # 使用 OpenAILike 绕过模型名校验
        self.llm = OpenAILike(
            model="deepseek-reasoner",
            api_key=api_key,
            api_base=api_base,
            max_tokens=max_tokens,
            is_chat_model=True,
            timeout=600,
            additional_kwargs={"extra_body": {"thinking": {"type": "enabled"}}}
        )

    def codegen(self, prompt: str, num_samples: int = 1, **kwargs) -> List[dict]:
        results = []
        for i in range(num_samples):
            # 执行预测
            response = self.llm.complete(prompt)
            
            # 1. 提取思维链 (DeepSeek R1 特有)
            reasoning = "No reasoning content found."
            p_tokens, c_tokens = 0, 0
            
            if hasattr(response, 'raw') and response.raw:
                try:
                    # 优先处理 Pydantic 对象 (OpenAI SDK 1.0+)
                    raw_msg = response.raw.choices[0].message
                    reasoning = getattr(raw_msg, 'reasoning_content', "Reasoning not found")
                    
                    # 抓取 Token 统计
                    usage = getattr(response.raw, 'usage', None)
                    if usage:
                        p_tokens = getattr(usage, 'prompt_tokens', 0)
                        c_tokens = getattr(usage, 'completion_tokens', 0)
                except Exception:
                    # 备选：处理字典格式
                    try:
                        raw_dict = response.raw if isinstance(response.raw, dict) else response.raw.model_dump()
                        msg_dict = raw_dict.get('choices', [{}])[0].get('message', {})
                        reasoning = msg_dict.get('reasoning_content', "Reasoning not found")
                        usage_dict = raw_dict.get('usage', {})
                        p_tokens = usage_dict.get('prompt_tokens', 0)
                        c_tokens = usage_dict.get('completion_tokens', 0)
                    except: pass

            # 在终端实时打印思维链，方便观测 Baseline 逻辑
            print(f"\n--- 🤔 DeepSeek R1 Reasoning (Sample {i+1}) ---\n{reasoning}\n")

            # 2. 封装为 Agentless 预期格式
            results.append({
                "response": response.text,
                "usage": {
                    "completion_tokens": c_tokens,
                    "prompt_tokens": p_tokens
                }
            })
        return results

    def is_direct_completion(self) -> bool:
        return False

# =================================================================
# --- 3. 模型工厂函数 ---
# =================================================================

def make_model(model, logger, backend, **kwargs):
    """
    统一模型生成入口。
    """
    api_key = os.getenv("DPSEEK_API_KEY")
    api_base = "https://api.deepseek.com"
    
    # 获取调用方指定的 tokens，默认 8192
    m_tokens = kwargs.pop('max_tokens', 8192)
    
    return DeepSeekR1Wrapper(
        api_key=api_key,
        api_base=api_base,
        max_tokens=m_tokens,
        model=model,
        logger=logger,
        **kwargs
    )

# 为保持原有代码库兼容，提供占位类
class OpenAIChatDecoder(DecoderBase):
    def codegen(self, *args, **kwargs): return []
    def is_direct_completion(self): return False

class AnthropicChatDecoder(DecoderBase):
    def codegen(self, *args, **kwargs): return []
    def is_direct_completion(self): return False
