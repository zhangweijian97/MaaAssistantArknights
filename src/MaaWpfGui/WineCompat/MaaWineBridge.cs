// <copyright file="MaaWineBridge.cs" company="MaaAssistantArknights">
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

using System.Runtime.InteropServices;

namespace MaaWpfGui.WineCompat;

internal static class MaaWineBridge
{
    // MaaWineBridge
    [DllImport("MaaCore.dll", CallingConvention = CallingConvention.Cdecl)]
    private static extern int dl_has_maacore();

    public static WineBridgeAvailability Availability { get; }

    static MaaWineBridge()
    {
        if (!WineRuntimeInformation.IsRunningUnderWine)
        {
            Availability = WineBridgeAvailability.NotAvailable;
            return;
        }

        try
        {
            if (dl_has_maacore() != 0)
            {
                Availability = WineBridgeAvailability.Operational;
            }
            else
            {
                Availability = WineBridgeAvailability.Faulted;
            }
        }
        catch
        {
            Availability = WineBridgeAvailability.NotAvailable;
        }
    }
}
