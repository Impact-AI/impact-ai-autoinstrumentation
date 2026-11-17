// SPDX-License-Identifier: Apache-2.0

// The workload's own startup hook: it says that it ran, nothing more.
internal static class StartupHook
{
    public static void Initialize() => System.Console.Error.WriteLine("own startup hook: ran");
}
