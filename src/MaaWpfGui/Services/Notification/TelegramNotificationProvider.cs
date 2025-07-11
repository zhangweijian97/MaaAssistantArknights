// <copyright file="TelegramNotificationProvider.cs" company="MaaAssistantArknights">
// Part of the MaaWpfGui project, maintained by the MaaAssistantArknights team (Maa Team)
// Copyright (C) 2021-2025 MaaAssistantArknights Contributors
//
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU Affero General Public License v3.0 only as published by
// the Free Software Foundation, either version 3 of the License, or
// any later version.
//
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY
// </copyright>

#nullable enable

using System;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading.Tasks;
using MaaWpfGui.Helper;
using MaaWpfGui.Services.Web;
using MaaWpfGui.ViewModels.UI;
using Serilog;

namespace MaaWpfGui.Services.Notification;

public class TelegramNotificationProvider(IHttpService httpService) : IExternalNotificationProvider
{
    private readonly ILogger _logger = Log.ForContext<TelegramNotificationProvider>();

    public async Task<bool> SendAsync(string title, string content)
    {
        var botToken = SettingsViewModel.ExternalNotificationSettings.TelegramBotToken;
        var chatId = SettingsViewModel.ExternalNotificationSettings.TelegramChatId;
        var topicId = SettingsViewModel.ExternalNotificationSettings.TelegramTopicId;

        var uri = $"https://api.telegram.org/bot{botToken}/sendMessage";

        var postContent = new TelegramPostContent
        {
            ChatId = chatId,
            Content = $"{title}: {content}",
        };

        // Only add the topic ID if one is provided
        if (!string.IsNullOrEmpty(topicId))
        {
            postContent.TopicId = topicId;
        }

        try
        {
            var response = await httpService.PostAsync(new(uri), new StringContent(JsonSerializer.Serialize(postContent), Encoding.UTF8, "application/json"), uriPartial: UriPartial.Authority);
            response.EnsureSuccessStatusCode();
            var str = await response.Content.ReadAsStringAsync();
            if (response is not null)
            {
                return !str.Contains("\"ok\":false");
            }
        }
        catch (Exception e)
        {
            _logger.Error(e, "Failed to send POST request to {Uri}", uri);
        }

        _logger.Warning("Failed to send message.");
        return false;
    }

    private class TelegramPostContent
    {
        [JsonPropertyName("chat_id")]
        public string? ChatId { get; set; }

        [JsonPropertyName("text")]
        public string? Content { get; set; }

        [JsonPropertyName("message_thread_id")]
        public string? TopicId { get; set; }
    }
}
