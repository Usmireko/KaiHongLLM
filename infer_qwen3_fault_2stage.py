import json
import os
import torch

from transformers import (
    AutoTokenizer,
    AutoModelForCausalLM,
    BitsAndBytesConfig,
)
from peft import PeftModel

BASE_MODEL = "/home/xrh/models/Qwen/Qwen3-8B"
ADAPTER_DIR = "/home/xrh/qwen3_os_fault/qwen3_8b_fault_qlora"
TEST_PATH = "/home/xrh/qwen3_os_fault/data/llm_sft_test.jsonl"
MAX_NEW_TOKENS = min(256, int(os.environ.get("QWEN3_MAX_NEW_TOKENS", "256")))


def load_test_samples(path: str):
    samples = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            samples.append(json.loads(line))
    return samples


def build_model(device_map=None):
    if device_map is None:
        device_map = os.environ.get("QWEN3_DEVICE_MAP", "auto")
    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL, use_fast=False)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
        bnb_4bit_compute_dtype=torch.bfloat16,
    )

    base_model = AutoModelForCausalLM.from_pretrained(
        BASE_MODEL,
        quantization_config=bnb_config,
        device_map=device_map,
        torch_dtype=torch.bfloat16,
    )

    model = PeftModel.from_pretrained(base_model, ADAPTER_DIR)
    model.eval()
    return tokenizer, model


def stage1_reason(tokenizer, model, messages_for_model):
    """第一阶段：让模型自由思考，输出 <think> + 详细分析"""
    input_ids = tokenizer.apply_chat_template(
        messages_for_model,
        tokenize=True,
        add_generation_prompt=True,
        return_tensors="pt",
    ).to(model.device)

    attention_mask = torch.ones_like(input_ids)

    with torch.no_grad():
        gen_ids = model.generate(
            input_ids=input_ids,
            attention_mask=attention_mask,
            max_new_tokens=MAX_NEW_TOKENS,
            do_sample=False,
            pad_token_id=tokenizer.pad_token_id,
            eos_token_id=tokenizer.eos_token_id,
        )

    gen_ids = gen_ids[0][input_ids.shape[-1]:]
    raw_pred = tokenizer.decode(gen_ids, skip_special_tokens=True)
    return raw_pred.strip()


def stage2_summarize(tokenizer, model, analysis_text):
    """第二阶段：把上面的分析再喂给模型，让它只输出精简后的诊断结果"""
    sys_msg = {
        "role": "system",
        "content": (
            "你是一个面向 KaiHongOS / OpenHarmony 的系统故障诊断助手。\n"
            "下面是一段关于某次 run 的详细分析文本，请你只根据这段分析，"
            "用【结构化格式】输出最终诊断结果，禁止输出<think>标签或任何额外解释。\n\n"
            "【输出格式（必须严格遵守）】\n"
            "1. 故障判定\n"
            "   - 故障状态: 是（故障）/ 否（正常）\n"
            "   - 故障家族: cpu / mem / background / other\n\n"
            "2. 根因分析\n"
            "   - 用 2–4 句话说明关键指标/进程/日志依据，可点名相关进程或组件。\n\n"
            "3. 建议的排查 / 恢复动作\n"
            "   - 给出 1–2 条可执行建议（可以是具体命令或操作步骤）。\n\n"
            "4. 诊断置信度: 0.xx\n\n"
            "【重要约束】\n"
            "1) 不要输出场景标签（如 cpu_oversub、mem_oomsafe、bg_idle），这些由系统在离线规则中自行映射；\n"
            "2) 若分析文本中未出现持续异常的 CPU/内存极值、严重内核/应用错误、"
            "   或频繁的 cpu_hotspot/mem_oom 类事件，则可倾向于判定为“否（正常）、background 家族”；\n"
            "3) 若分析文本中强调 memory_leak_demo 或其他进程 RSS 持续增长、mem_free_kb 接近 OOM，"
            "   且为主要问题，则更倾向于故障家族: mem；\n"
            "4) 若分析文本中强调 CPU 利用率长期接近 100% 或多次 cpu_hotspot，且为主要问题，则更倾向于故障家族: cpu。\n"
        ),
    }

    user_msg = {
        "role": "user",
        "content": (
            "以下是模型对某次 run 的详细分析，请你据此给出最终诊断结果：\n\n"
            f"{analysis_text}\n\n"
            "请严格按照上面的结构化格式输出，不要添加其它内容，也不要输出<think>。"
        ),
    }

    messages = [sys_msg, user_msg]

    input_ids = tokenizer.apply_chat_template(
        messages,
        tokenize=True,
        add_generation_prompt=True,
        return_tensors="pt",
    ).to(model.device)

    attention_mask = torch.ones_like(input_ids)

    with torch.no_grad():
        gen_ids = model.generate(
            input_ids=input_ids,
            attention_mask=attention_mask,
            max_new_tokens=MAX_NEW_TOKENS,
            do_sample=False,
            pad_token_id=tokenizer.pad_token_id,
            eos_token_id=tokenizer.eos_token_id,
        )

    gen_ids = gen_ids[0][input_ids.shape[-1]:]
    summ_pred = tokenizer.decode(gen_ids, skip_special_tokens=True)
    return summ_pred.strip()

def main():
    tokenizer, model = build_model()
    samples = load_test_samples(TEST_PATH)
    print(f"[INFO] Loaded {len(samples)} test samples from {TEST_PATH}")

    for idx, sample in enumerate(samples, start=1):
        messages_full = sample.get("messages", [])
        if not messages_full:
            continue

        # 拆 system + user / gold assistant
        gold_answer = ""
        messages_for_model = []
        for m in messages_full:
            role = m.get("role")
            if role == "assistant":
                if not gold_answer:
                    gold_answer = m.get("content", "")
            else:
                messages_for_model.append(m)

        if not messages_for_model:
            continue

        # 取 run_id 方便你看
        try:
            user_content = next(m["content"] for m in messages_for_model if m["role"] == "user")
        except StopIteration:
            user_content = messages_for_model[-1]["content"]
        run_id_line = ""
        for line in user_content.splitlines():
            if "【run_id】" in line:
                run_id_line = line.strip()
                break

        print("=" * 80)
        print(f"[Sample #{idx}]")
        if run_id_line:
            print(run_id_line)

        # 第一阶段：模型自由 think
        analysis = stage1_reason(tokenizer, model, messages_for_model)
        print("\n--- 阶段1：模型详细分析（原样）---")
        print(analysis)

        # 第二阶段：让模型把上面的分析压缩成结构化结论
        summary = stage2_summarize(tokenizer, model, analysis)
        print("\n--- 阶段2：结构化诊断结果 ---")
        print(summary)

        if gold_answer:
            print("\n--- 标准答案（label）---")
            print(gold_answer.strip())

    print("=" * 80)
    print("[INFO] Done.")


if __name__ == "__main__":
    main()
