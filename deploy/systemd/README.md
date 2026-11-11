# ZOps 系统服务（systemd --user）

安装（无需 root，开机自启 + 崩溃自动重启）：

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/zops-*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now zops-hub.service zops-keeper.service
loginctl enable-linger "$USER"      # 注销后仍保持用户服务(已开可跳过)
```

常用：

```bash
systemctl --user status  zops-hub zops-keeper
systemctl --user restart zops-hub
journalctl --user -u zops-hub -f        # 网关日志(也可 tail logs/hub.log)
```

为什么需要它：tmux 服务器被清理 / 进程被误杀时，tmux 里的实例、keeper、`nohup` 的网关
会**同时消失**（`nohup` 挡不住 SIGKILL）。交给 systemd 后 `Restart=always` 会自动拉起。

注意：`serial_keeper.py` 仍用 tmux 承载各串口实例（方便 attach 看日志）；
若 tmux 服务器整个消失，keeper 会在下一轮（≤3s）自动重建实例会话。
