#include "OperatorSkillMasteryImageAnalyzer.h"

#include "Utils/Logger.hpp"
#include <opencv2/imgproc.hpp>
#include <opencv2/imgcodecs.hpp>

namespace asst {

OperatorSkillMasteryImageAnalyzer::OperatorSkillMasteryImageAnalyzer(const cv::Mat& image)
    : m_image(image.clone()) {}

bool OperatorSkillMasteryImageAnalyzer::analyze()
{
    LogTraceFunction;

    if (m_image.empty()) {
        Log.error("OperatorSkillMasteryImageAnalyzer: empty image");
        return false;
    }

    // TODO: 实现具体的图像识别逻辑。
    // 1. 检测干员名称区域 (OCR 或模板匹配)
    // 2. 检测技能图标及专精小标识 (模板匹配)
    // 3. 填充 m_result

    return true;
}

} // namespace asst
