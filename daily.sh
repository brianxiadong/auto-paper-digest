#!/bin/bash

# 每日论文抖音发布脚本
# Daily Paper Douyin Publishing Script

# 获取今天的日期
TODAY=$(date +%Y-%m-%d)
MAX_PAPERS=5

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║           每日论文抖音发布工具 - Daily Paper Publisher        ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║  今日日期: $TODAY                                            ║"
echo "║  论文数量: $MAX_PAPERS 篇                                     ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║  请选择要执行的步骤:                                          ║"
echo "║                                                              ║"
echo "║  [1] 下载论文 + 生成视频 (fetch + download + upload)         ║"
echo "║  [2] 下载视频 + 发布抖音 (download-video + publish-douyin)   ║"
echo "║                                                              ║"
echo "║  [0] 退出                                                    ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# 支持命令行参数，用于非交互式执行（如 crontab）
if [ -n "$1" ]; then
    choice=$1
    echo "自动执行模式，选择: $choice"
else
    read -p "请输入步骤编号 (0-2): " choice
fi

case $choice in
    1)
        echo ""
        echo "=========================================="
        echo "📚 步骤 1: 下载论文 + 生成视频"
        echo "=========================================="
        echo ""
        echo "正在执行: apd upload --date $TODAY --max $MAX_PAPERS --headful"
        echo ""
        apd upload --date "$TODAY" --max "$MAX_PAPERS" --headful
        echo ""
        echo "✅ 步骤 1 完成！请等待几分钟让视频生成，然后执行步骤 2"
        ;;
    2)
        echo ""
        echo "=========================================="
        echo "📥 步骤 2a: 下载视频"
        echo "=========================================="
        echo ""
        echo "正在执行: apd download-video --date $TODAY --headful"
        echo ""
        apd download-video --date "$TODAY" --headful
        
        echo ""
        echo "=========================================="
        echo "🎬 步骤 2b: 发布抖音"
        echo "=========================================="
        echo ""
        echo "正在执行: apd publish-douyin --date $TODAY --headful"
        echo ""
        apd publish-douyin --date "$TODAY" --headful
        
        echo ""
        echo "🎉 全部完成！"
        ;;
    0)
        echo "👋 再见!"
        exit 0
        ;;
    *)
        echo "❌ 无效选择"
        exit 1
        ;;
esac
