# id_updater.py
#
# 这是一个经过升级的循环HTTP服务器，用于根据用户选择的模式
# (DirectChat 或 Battle) 接收来自油猴脚本的会话信息，
# 并将其更新到 config.jsonc 或 model_endpoint_map.json 文件中。
#
# === LMArena 模式说明 ===
# LMArena 网站有三种对话模式：
#
# 1. Direct (直接对话)
#    - 与单个已知模型对话
#    - 使用本工具的 DirectChat 模式捕获
#    - search 类型模型在 DirectChat 模式下使用
#
# 2. Side by Side (并排对比)
#    - 同时与两个已知模型对话（非匿名）
#    - 使用本工具的 Battle 模式捕获
#    - 选择 A 或 B 表示左侧或右侧模型位置
#
# 3. Battle (对战模式)
#    - 同时与两个匿名模型对话
#    - 使用本工具的 Battle 模式捕获
#    - 选择 A 或 B 表示左侧或右侧模型位置
#    - 这是唯一真正使用匿名模型的模式
#
# === 自动保存模式 ===
# 支持三种自动保存模式：
# - 'model': 自动保存到特定模型 (推荐)
# - 'global': 保存到全局配置
# - 'ask': 每次询问

import http.server
import socketserver
import json
import re
import threading
import os
import requests
import time

# --- 配置常量 ---
HOST = "127.0.0.1"
PORT = 5103
CONFIG_PATH = 'config.jsonc'
MODEL_ENDPOINT_MAP_PATH = 'model_endpoint_map.json'

# 有效的自动保存模式
VALID_AUTO_SAVE_MODES = ['model', 'global', 'ask']

def read_config():
    """读取并解析 config.jsonc 文件，移除注释以便解析。"""
    if not os.path.exists(CONFIG_PATH):
        print(f"❌ 错误：配置文件 '{CONFIG_PATH}' 不存在。")
        return None
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        # 更稳健地移除注释，逐行处理以避免错误删除URL中的 "//"
        no_comments_lines = []
        in_block_comment = False
        for line in lines:
            stripped_line = line.strip()
            if in_block_comment:
                if '*/' in stripped_line:
                    in_block_comment = False
                    line = stripped_line.split('*/', 1)[1]
                else:
                    continue
            
            if '/*' in line and not in_block_comment:
                before_comment, _, after_comment = line.partition('/*')
                if '*/' in after_comment:
                    _, _, after_block = after_comment.partition('*/')
                    line = before_comment + after_block
                else:
                    line = before_comment
                    in_block_comment = True

            if line.strip().startswith('//'):
                continue
            
            no_comments_lines.append(line)

        json_content = "".join(no_comments_lines)
        return json.loads(json_content)
    except Exception as e:
        print(f"❌ 读取或解析 '{CONFIG_PATH}' 时发生错误: {e}")
        return None

def save_config_value(key, value):
    """
    安全地更新 config.jsonc 中的单个键值对，保留原始格式和注释。
    仅适用于值为字符串或数字的情况。
    """
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            content = f.read()

        # 使用正则表达式安全地替换值
        # 它会查找 "key": "any value" 并替换 "any value"
        pattern = re.compile(rf'("{key}"\s*:\s*")[^"]*(")')
        new_content, count = pattern.subn(rf'\g<1>{value}\g<2>', content, 1)

        if count == 0:
            print(f"🤔 警告: 未能在 '{CONFIG_PATH}' 中找到键 '{key}'。")
            return False

        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            f.write(new_content)
        return True
    except Exception as e:
        print(f"❌ 更新 '{CONFIG_PATH}' 时发生错误: {e}")
        return False

def save_session_ids(session_id, message_id):
    """将新的会话ID更新到 config.jsonc 文件。"""
    print(f"\n📝 正在尝试将ID写入 '{CONFIG_PATH}'...")
    res1 = save_config_value("session_id", session_id)
    res2 = save_config_value("message_id", message_id)
    if res1 and res2:
        print(f"✅ 成功更新ID。")
        print(f"   - session_id: {session_id}")
        print(f"   - message_id: {message_id}")
        return True
    else:
        print(f"❌ 更新ID失败。请检查上述错误信息。")
        return False

def read_model_endpoint_map():
    """读取 model_endpoint_map.json 文件。"""
    if not os.path.exists(MODEL_ENDPOINT_MAP_PATH):
        return {}
    try:
        with open(MODEL_ENDPOINT_MAP_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"❌ 读取 '{MODEL_ENDPOINT_MAP_PATH}' 时发生错误: {e}")
        return {}

def save_model_endpoint_map(data):
    """保存 model_endpoint_map.json 文件。"""
    try:
        with open(MODEL_ENDPOINT_MAP_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"❌ 保存 '{MODEL_ENDPOINT_MAP_PATH}' 时发生错误: {e}")
        return False

def save_to_model_endpoint(model_name, session_id, message_id, mode, battle_target=None, model_type=None):
    """将捕获的ID保存到 model_endpoint_map.json 中特定模型的配置。"""
    print(f"\n📝 正在将ID写入 '{MODEL_ENDPOINT_MAP_PATH}' 的模型 '{model_name}' 配置...")
    
    endpoint_map = read_model_endpoint_map()
    
    # 构建配置条目
    entry = {
        "session_id": session_id,
        "message_id": message_id,
        "mode": mode
    }
    
    if model_type:
        entry["type"] = model_type
    
    if mode == "battle" and battle_target:
        entry["battle_target"] = battle_target
    
    endpoint_map[model_name] = entry
    
    if save_model_endpoint_map(endpoint_map):
        print(f"✅ 成功保存模型配置！")
        print(f"   - 模型名称: {model_name}")
        print(f"   - session_id: {session_id}")
        print(f"   - message_id: {message_id}")
        print(f"   - mode: {mode}")
        if model_type:
            print(f"   - type: {model_type}")
        if mode == "battle":
            print(f"   - battle_target: {battle_target}")
        return True
    else:
        print(f"❌ 保存失败。")
        return False

def get_configured_models():
    """获取已在 model_endpoint_map.json 中配置的模型列表。"""
    endpoint_map = read_model_endpoint_map()
    return list(endpoint_map.keys())

# 全局变量用于存储捕获的ID
captured_data = {}

class RequestHandler(http.server.SimpleHTTPRequestHandler):
    def _send_cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_POST(self):
        if self.path == '/update':
            try:
                content_length = int(self.headers['Content-Length'])
                post_data = self.rfile.read(content_length)
                data = json.loads(post_data)

                session_id = data.get('sessionId')
                message_id = data.get('messageId')

                if session_id and message_id:
                    print("\n" + "=" * 50)
                    print("🎉 成功从浏览器捕获到ID！")
                    print(f"  - Session ID: {session_id}")
                    print(f"  - Message ID: {message_id}")
                    print("=" * 50)

                    # 将捕获的数据存储到全局变量
                    captured_data['session_id'] = session_id
                    captured_data['message_id'] = message_id

                    # 🎯 新增：同时通知api_server（端口5102），让admin面板能检测到
                    try:
                        print("\n📡 正在通知主服务器...")
                        notify_response = requests.post(
                            'http://127.0.0.1:5102/internal/receive_captured_ids',
                            json={'sessionId': session_id, 'messageId': message_id},
                            timeout=3
                        )
                        if notify_response.status_code == 200:
                            print("✅ 已成功通知主服务器（admin面板可见）")
                        else:
                            print(f"⚠️  通知主服务器失败: HTTP {notify_response.status_code}")
                    except requests.ConnectionError:
                        print("⚠️  无法连接到主服务器（端口5102），admin面板将无法显示捕获结果")
                        print("   - 提示：确保 api_server.py 正在运行")
                    except Exception as e:
                        print(f"⚠️  通知主服务器时出错: {e}")

                    self.send_response(200)
                    self._send_cors_headers()
                    self.end_headers()
                    self.wfile.write(b'{"status": "success"}')

                    print("\n✅ ID已捕获，服务器将在1秒后自动关闭。")
                    threading.Thread(target=self.server.shutdown).start()

                else:
                    self.send_response(400, "Bad Request")
                    self._send_cors_headers()
                    self.end_headers()
                    self.wfile.write(b'{"error": "Missing sessionId or messageId"}')
            except Exception as e:
                self.send_response(500, "Internal Server Error")
                self._send_cors_headers()
                self.end_headers()
                self.wfile.write(f'{{"error": "Internal server error: {e}"}}'.encode('utf-8'))
        else:
            self.send_response(404, "Not Found")
            self._send_cors_headers()
            self.end_headers()

    def log_message(self, format, *args):
        return

def run_server():
    with socketserver.TCPServer((HOST, PORT), RequestHandler) as httpd:
        print("\n" + "="*50)
        print("  🚀 会话ID更新监听器已启动")
        print(f"  - 监听地址: http://{HOST}:{PORT}")
        print("  - 请在浏览器中操作LMArena页面以触发ID捕获。")
        print("  - 捕获成功后，此脚本将自动关闭。")
        print("="*50)
        httpd.serve_forever()

def notify_api_server():
    """通知主 API 服务器，ID 更新流程已开始。"""
    api_server_url = "http://127.0.0.1:5102/internal/start_id_capture"
    try:
        response = requests.post(api_server_url, timeout=3)
        if response.status_code == 200:
            print("✅ 已成功通知主服务器激活ID捕获模式。")
            return True
        else:
            print(f"⚠️ 通知主服务器失败，状态码: {response.status_code}。")
            print(f"   - 错误信息: {response.text}")
            return False
    except requests.ConnectionError:
        print("❌ 无法连接到主 API 服务器。请确保 api_server.py 正在运行。")
        return False
    except Exception as e:
        print(f"❌ 通知主服务器时发生未知错误: {e}")
        return False

def process_captured_ids(session_id, message_id, mode, battle_target, auto_save_mode):
    """
    处理捕获的ID，根据自动保存模式决定如何保存。
    支持保存失败后的重试功能。
    """
    endpoint_map = read_model_endpoint_map()
    configured_models = list(endpoint_map.keys())
    
    # 如果是 'model' 模式，直接询问模型名称
    if auto_save_mode == "model":
        # 显示已配置的模型（供参考）
        if configured_models:
            print("\n💡 已配置的模型列表：")
            for i, model in enumerate(configured_models, 1):
                print(f"   {i}. {model}")
        
        while True:  # 循环直到保存成功或用户跳过
            model_name = input("\n请输入模型名称 (直接回车跳过): ").strip()
            
            if not model_name:
                print("⏭️  已跳过，准备下一次捕获...")
                return True
            
            # 询问模型类型
            print("\n请选择模型类型：")
            print("  1. text   - 文本模型 (默认)")
            print("  2. image  - 图像模型")
            print("  3. search - 搜索模型")
            type_choice = input("请输入选项 [1/2/3] (默认: 1): ").strip()
            
            type_map = {"1": None, "2": "image", "3": "search", "": None}
            model_type = type_map.get(type_choice, None)
            
            # 如果是 search 模型且在 Battle 模式，建议选择 A
            if model_type == "search" and mode == "battle":
                if battle_target != "A":
                    print("💡 提示：search 模型通常在 DirectChat 模式下使用")
                    print("   如果在 Battle/Side by Side 模式下使用，建议选择目标 A")
                    confirm = input("当前目标为 B，是否继续? [y/N]: ").lower().strip()
                    if confirm != 'y':
                        continue
            
            # 确认是否覆盖
            if model_name in endpoint_map:
                confirm = input(f"⚠️  模型 '{model_name}' 已存在配置，是否覆盖? [y/N]: ").lower().strip()
                if confirm != 'y':
                    print("⏭️  已跳过，准备下一次捕获...")
                    return True
            
            # 保存到 model_endpoint_map.json
            if save_to_model_endpoint(model_name, session_id, message_id, mode, battle_target, model_type):
                print(f"\n🎉 配置完成！模型 '{model_name}' 现在可以使用了。")
                return True
            else:
                # 保存失败，询问是否重试
                retry = input("\n保存失败，是否重试? [Y/n]: ").lower().strip()
                if retry == 'n':
                    print("⏭️  已取消，准备下一次捕获...")
                    return False
                # 继续循环，重新尝试
    
    # 如果是 'global' 模式，直接保存到全局配置
    elif auto_save_mode == "global":
        while True:  # 支持重试
            if save_session_ids(session_id, message_id):
                print("\n✅ 全局配置已更新。")
                return True
            else:
                # 保存失败，询问是否重试
                retry = input("\n保存失败，是否重试? [Y/n]: ").lower().strip()
                if retry == 'n':
                    print("⏭️  已取消，准备下一次捕获...")
                    return False
    
    # 如果是 'ask' 模式，显示选择菜单
    else:
        while True:  # 外层循环用于处理保存失败后的重试
            print("\n" + "=" * 50)
            print("📋 请选择要如何保存这些ID：")
            print("=" * 50)
            
            # 显示已配置的模型列表
            if configured_models:
                print("\n💡 已配置的模型：")
                for i, model in enumerate(configured_models, 1):
                    print(f"   {i}. {model}")
            
            print("\n请选择操作：")
            print("  1. 为特定模型配置这些ID (推荐)")
            print("  2. 更新全局默认ID (config.jsonc)")
            print("  3. 跳过")
            
            action_choice = input("\n请输入选项 [1/2/3]: ").strip()
            
            if action_choice == "1":
                model_name = input("\n请输入模型名称 (例如: gpt-5-high): ").strip()
                
                if not model_name:
                    print("❌ 模型名称不能为空。")
                    continue  # 重新显示菜单
                
                # 询问模型类型
                print("\n请选择模型类型：")
                print("  1. text   - 文本模型 (默认)")
                print("  2. image  - 图像模型")
                print("  3. search - 搜索模型")
                type_choice = input("请输入选项 [1/2/3] (默认: 1): ").strip()
                
                type_map = {"1": None, "2": "image", "3": "search", "": None}
                model_type = type_map.get(type_choice, None)
                
                # 如果是 search 模型且在 Battle 模式，建议选择 A
                if model_type == "search" and mode == "battle":
                    if battle_target != "A":
                        print("💡 提示：search 模型通常在 DirectChat 模式下使用")
                        print("   如果在 Battle/Side by Side 模式下使用，建议选择目标 A")
                        confirm_warn = input("当前目标为 B，是否继续? [y/N]: ").lower().strip()
                        if confirm_warn != 'y':
                            continue
                
                if model_name in endpoint_map:
                    confirm = input(f"⚠️  模型 '{model_name}' 已存在配置，是否覆盖? [y/N]: ").lower().strip()
                    if confirm != 'y':
                        print("⏭️  已跳过。")
                        return True
                
                if save_to_model_endpoint(model_name, session_id, message_id, mode, battle_target, model_type):
                    print(f"\n🎉 配置完成！模型 '{model_name}' 现在可以使用了。")
                    return True
                else:
                    # 保存失败，询问是否重试
                    retry = input("\n保存失败，是否重新选择操作? [Y/n]: ").lower().strip()
                    if retry == 'n':
                        print("\n⏭️  已取消。")
                        return False
                    # 继续外层循环，重新显示菜单
            
            elif action_choice == "2":
                confirm = input("⚠️  确认要更新全局配置? [y/N]: ").lower().strip()
                if confirm == 'y':
                    if save_session_ids(session_id, message_id):
                        print("\n✅ 全局配置已更新。")
                        return True
                    else:
                        # 保存失败，询问是否重试
                        retry = input("\n保存失败，是否重新选择操作? [Y/n]: ").lower().strip()
                        if retry == 'n':
                            print("\n⏭️  已取消。")
                            return False
                        # 继续外层循环
                else:
                    print("⏭️  已取消。")
                    return True
            
            else:
                print("\n⏭️  已跳过。")
                return True

if __name__ == "__main__":
    config = read_config()
    if not config:
        exit(1)
    
    # 显示欢迎信息
    print("\n" + "=" * 60)
    print("  🚀 LMArena 模型配置工具 (循环模式)")
    print("=" * 60)
    print("  提示：完成一次配置后会自动继续，按 Ctrl+C 退出")
    print()
    
    # 显示 LMArena 模式说明
    print("  📖 LMArena 模式说明")
    print("  " + "-" * 56)
    print("  1️⃣  Direct (直接对话)")
    print("      - 与单个已知模型对话")
    print("      - 使用本工具的 DirectChat 模式捕获")
    print("      - search 类型模型在 DirectChat 模式下使用")
    print()
    print("  2️⃣  Side by Side (并排对比)")
    print("      - 同时与两个已知模型对话（非匿名）")
    print("      - 使用本工具的 Battle 模式捕获")
    print("      - 选择 A 或 B 表示左侧或右侧模型位置")
    print()
    print("  3️⃣  Battle (对战模式)")
    print("      - 同时与两个匿名模型对话")
    print("      - 使用本工具的 Battle 模式捕获")
    print("      - 选择 A 或 B 表示左侧或右侧模型位置")
    print("      - 这是唯一真正使用匿名模型的模式")
    print("  " + "-" * 56)
    print()
    
    # 读取并验证自动保存模式
    auto_save_mode = config.get("id_updater_auto_save_mode", "model")
    if auto_save_mode not in VALID_AUTO_SAVE_MODES:
        print(f"  ⚠️  配置中的 'id_updater_auto_save_mode' 值无效: '{auto_save_mode}'")
        print(f"  ⚠️  有效值为: {', '.join(VALID_AUTO_SAVE_MODES)}")
        print(f"  ⚠️  将使用默认值: 'model'")
        auto_save_mode = "model"
    
    mode_desc = {
        "model": "自动保存到特定模型",
        "global": "自动保存到全局配置",
        "ask": "每次询问"
    }
    print(f"  ⚙️  当前自动保存模式: {mode_desc.get(auto_save_mode, auto_save_mode)}")
    print(f"      (可在 config.jsonc 中修改 'id_updater_auto_save_mode')")
    
    # 显示统计信息
    configured_models = get_configured_models()
    print(f"  📊 当前已配置 {len(configured_models)} 个模型")
    
    print("=" * 60)

    try:
        while True:  # 循环执行
            # 清空上次捕获的数据
            captured_data.clear()
            
            # --- 获取用户选择 ---
            last_mode = config.get("id_updater_last_mode", "direct_chat")
            mode_map = {"a": "direct_chat", "b": "battle", "q": "quit"}
            
            print("\n")
            prompt = f"请选择模式 [a: DirectChat, b: Battle, q: 退出] (默认: {last_mode}): "
            choice = input(prompt).lower().strip()
            
            if choice == "q":
                print("\n👋 再见！")
                break

            if not choice:
                mode = last_mode
            else:
                mode = mode_map.get(choice)
                if not mode or mode == "quit":
                    print("\n👋 再见！")
                    break
                if not mode:
                    print(f"无效输入，将使用默认值: {last_mode}")
                    mode = last_mode

            save_config_value("id_updater_last_mode", mode)
            print(f"✅ 当前模式: {mode.upper()}")
            
            battle_target = None
            if mode == 'battle':
                print("\n💡 说明：在 Battle 或 Side by Side 模式下")
                print("   - A 表示左侧模型位置")
                print("   - B 表示右侧模型位置")
                last_target = config.get("id_updater_battle_target", "A")
                target_prompt = f"请选择目标 [A 或 B] (默认: {last_target}): "
                target_choice = input(target_prompt).upper().strip()

                if not target_choice:
                    battle_target = last_target
                elif target_choice in ["A", "B"]:
                    battle_target = target_choice
                else:
                    print(f"无效输入，将使用默认值: {last_target}")
                    battle_target = last_target
                
                save_config_value("id_updater_battle_target", battle_target)
                print(f"✅ Battle 目标: {battle_target} (左侧模型)" if battle_target == "A" else f"✅ Battle 目标: {battle_target} (右侧模型)")

            # 在启动监听之前，先通知主服务器
            if not notify_api_server():
                print("\n⚠️  无法通知主服务器，请确保 api_server.py 正在运行。")
                retry = input("是否重试? [y/N]: ").lower().strip()
                if retry != 'y':
                    continue
                else:
                    if not notify_api_server():
                        print("❌ 仍然无法连接，跳过此次捕获。")
                        continue
            
            # 启动服务器捕获ID
            run_server()
            print("服务器已关闭。")
            
            # 检查是否成功捕获了ID
            if 'session_id' not in captured_data or 'message_id' not in captured_data:
                print("⚠️  未能捕获到有效的ID。")
                retry = input("是否重新开始? [Y/n]: ").lower().strip()
                if retry == 'n':
                    break
                continue
            
            session_id = captured_data['session_id']
            message_id = captured_data['message_id']
            
            # 处理捕获的ID
            process_captured_ids(session_id, message_id, mode, battle_target, auto_save_mode)
            
            # 重新读取配置和统计信息（用户可能在运行中修改了配置）
            config = read_config()
            if config:
                auto_save_mode = config.get("id_updater_auto_save_mode", "model")
                if auto_save_mode not in VALID_AUTO_SAVE_MODES:
                    auto_save_mode = "model"
            
            # 更新并显示统计信息
            configured_models = get_configured_models()
            
            print("\n" + "-" * 60)
            print(f"📊 已配置 {len(configured_models)} 个模型")
            print("✅ 准备下一次捕获...")
            print("-" * 60)
    
    except KeyboardInterrupt:
        print("\n\n👋 已手动中断，再见！")
        exit(0)