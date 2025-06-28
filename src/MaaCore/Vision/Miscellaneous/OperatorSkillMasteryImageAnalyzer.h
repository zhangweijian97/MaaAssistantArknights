#pragma once

#include <opencv2/core.hpp>
#include <string>
#include <vector>

namespace asst {

/**
 * OperatorSkillMasteryImageAnalyzer
 * 负责在干员技能界面截图中识别干员名称、技能序号与专精等级。
 * 当前实现仅提供接口定义，后续将补充具体识别逻辑。
 */
class OperatorSkillMasteryImageAnalyzer {
public:
    struct SkillInfo {
        int skill_index = 1;      // 技能序号：1/2/3
        int mastery_level = 0;    // 专精等级：0=未专,1,2,3
    };

    struct OperatorInfo {
        std::string name;              // 干员名称
        std::vector<SkillInfo> skills; // 识别到的技能信息
    };

public:
    explicit OperatorSkillMasteryImageAnalyzer(const cv::Mat& image);

    /**
     * 执行识别流程。
     * @return true 识别成功，false 识别失败或场景不匹配。
     */
    bool analyze();

    /**
     * 获取识别结果。
     */
    const std::vector<OperatorInfo>& get_result() const noexcept { return m_result; }

private:
    cv::Mat m_image;                      // 原始截图
    std::vector<OperatorInfo> m_result;   // 识别结果缓存
};

} // namespace asst
