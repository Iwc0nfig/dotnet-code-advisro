#if NET48
namespace System.Runtime.CompilerServices
{
    /// <summary>
    /// Shim so that C# records and init-only setters compile against .NET
    /// Framework 4.8, which predates this type.  The compiler only needs the
    /// type to exist; it emits no code that depends on its contents.
    /// </summary>
    internal static class IsExternalInit
    {
    }
}
#endif
