import { createContext } from "react";
import { type ProfileField } from "../api";

// 空间配置的画像字段（中文名与说明），由报告顶层提供给所有工具调用块与已知画像
export const ProfileFields = createContext<Record<string, ProfileField>>({});
