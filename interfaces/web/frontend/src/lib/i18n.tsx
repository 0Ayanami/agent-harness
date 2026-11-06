"use client";
import { createContext, useContext, ReactNode } from "react";
import en from "@/i18n/messages/en.json";
import zh from "@/i18n/messages/zh.json";
import ja from "@/i18n/messages/ja.json";
import { runtimeMessages } from "@/i18n/runtime-messages";
import { libraryMessages } from "@/i18n/library-messages";
import { ragMessages } from "@/i18n/rag-messages";
import { skillMessages } from "@/i18n/skill-messages";
import { scheduleMessages } from "@/i18n/schedule-messages";

type Messages = typeof en;

const messagesMap: Record<string, Messages> = { en, zh, ja };

const I18nContext = createContext<{ locale: string; messages: Messages }>({
  locale: "en",
  messages: en,
});

export function I18nProvider({ locale, children }: { locale: string; children: ReactNode }) {
  const messages = messagesMap[locale] || en;
  return (
    <I18nContext.Provider value={{ locale, messages }}>
      {children}
    </I18nContext.Provider>
  );
}

export function useTranslations(namespace?: string) {
  const { locale, messages } = useContext(I18nContext);
  return (key: string) => {
    if (namespace === "schedule") {
      return scheduleMessages[locale]?.[key] || scheduleMessages.en[key] || key;
    }
    if (namespace === "skills") {
      return skillMessages[locale]?.[key] || skillMessages.en[key] || key;
    }
    if (namespace === "rag") {
      return ragMessages[locale]?.[key] || ragMessages.en[key] || key;
    }
    if (namespace === "runtime") {
      return runtimeMessages[locale]?.[key] || runtimeMessages.en[key] || key;
    }
    if (namespace === "library") {
      return libraryMessages[locale]?.[key] || libraryMessages.en[key] || key;
    }
    const dictionary = messages as Record<string, unknown>;
    const ns = namespace ? dictionary[namespace] : dictionary;
    if (!ns || typeof ns !== "object") return key;
    const value = (ns as Record<string, unknown>)[key];
    return typeof value === "string" ? value : key;
  };
}

export function useLocale() {
  return useContext(I18nContext).locale;
}
