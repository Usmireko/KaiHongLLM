import json
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


def load_test_samples(path: str):
    samples = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            samples.append(obj)
    return samples


def extract_structured_answer(text: str) -> str:
    """
    尝试从输出中截取从“1. 故障判定与家族”开始的结构化部分。
    如果找不到，就返回原文。
    """
    marker = "1. 故障判定与家族"
    idx = text.find(marker)
    if idx == -1:
        return text.strip()
    return text[idx:].strip()


def main():
    # ===== 1. 4bit 量化加载基座 + 挂 LoRA =====
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
        device_map="auto",
        torch_dtype=torch.bfloat16,
    )

    model = PeftModel.from_pretrained(
        base_model,
        ADAPTER_DIR,
    )
    model.eval()

    # ===== 2. 读测试数据 =====
    samples = load_test_samples(TEST_PATH)
    print(f"[INFO] Loaded {len(samples)} test samples from {TEST_PATH}")

    for idx, sample in enumerate(samples, start=1):
        messages_full = sample.get("messages", [])
        if not messages_full:
            continue

        gold_answer = ""
        messages_for_model = []

        for m in messages_full:
            role = m.get("role")
            if role == "assistant":
                if not gold_answer:
                    gold_answer = m.get("content", "")
            elif role == "system":
                # 用更强的 system 约束，禁止 <think>，要求结构化输出
                new_sys = {
                    "role": "system",
                    "content": (
                        "你是一个面向 KaiHongOS / OpenHarmony 的系统故障诊断助手。\n"
                        "【重要要求】禁止输出<think>标签或任何中间推理过程，只按照以下结构化格式回答：\n"
                        "1. 故障判定与家族\n"
                        "   - 故障状态: 是/否（可注明“故障/正常”）\n"
                        "   - 故障家族: cpu/mem/background/other\n"
                        "   - 场景标签: 场景名称\n"
                        "   - 严重程度: normal/warning/critical\n\n"
                        "2. 根因分析\n"
                        "   - 用2-4句话说明关键指标/进程/日志依据。\n\n"
                        "3. 建议的排查 / 恢复动作\n"
                        "   - 给出1-2条可执行建议。\n\n"
                        "4. 诊断置信度: 0.xx\n"
                    ),
                }
                messages_for_model.append(new_sys)
            else:
                messages_for_model.append(m)

        if not messages_for_model:
            continue

        # 3. chat_template 组装输入
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
                max_new_tokens=768,      # 比 512 再放大一点，给它空间写完结构
                do_sample=False,
                pad_token_id=tokenizer.pad_token_id,
                eos_token_id=tokenizer.eos_token_id,
            )

        # 只取新生成部分
        gen_ids = gen_ids[0][input_ids.shape[-1]:]
        raw_pred = tokenizer.decode(gen_ids, skip_special_tokens=True)

        structured_pred = extract_structured_answer(raw_pred)

        print("=" * 80)
        print(f"[Sample #{idx}]")

        # 把 run_id 打出来方便对照
        try:
            user_content = next(m["content"] for m in messages_for_model if m["role"] == "user")
        except StopIteration:
            user_content = messages_for_model[-1]["content"]
        run_id_line = ""
        for line in user_content.splitlines():
            if "【run_id】" in line:
                run_id_line = line.strip()
                break
        if run_id_line:
            print(run_id_line)

        print("\n--- 模型预测（裁剪后）---")
        print(structured_pred)

        print("\n--- 模型预测（原始）---")
        print(raw_pred.strip())

        if gold_answer:
            print("\n--- 标准答案（label）---")
            print(gold_answer.strip())

    print("=" * 80)
    print("[INFO] Done.")


if __name__ == "__main__":
    main()
