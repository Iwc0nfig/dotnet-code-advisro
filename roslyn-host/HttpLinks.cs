using System.Text;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;

namespace DotnetCodeAdvisor.RoslynHost;

/// <summary>
/// The two ends of an HTTP hop, as far as the compiler can see them.
///
/// HTTP clients provide URL literals and HTTP methods;
/// APIs declare routes through minimal-API routing or
/// attribute-routed controllers.  The compiler cannot connect the two -- the
/// connection is a URL string -- but it can give both ends exactly: which
/// HttpMethod, which literal URL parts, which handler method (by symbol).
/// Matching the URL to the route is done afterwards (index/api_links.py) and
/// is labelled as a route match, never as a compiler reference.
/// </summary>
internal static class HttpLinks
{
    public sealed class Endpoint
    {
        public string Verb = "";
        public string Route = "";
        public bool PrefixComplete = true;
        public ISymbol? Handler;
        public string HandlerKind = "method";   // method | lambda
        public string Style = "minimal";        // minimal | controller
        public SyntaxNode Node = null!;
        /// <summary>The type the handler takes as its request body, if any.</summary>
        public ITypeSymbol? Body;
    }

    public sealed class Call
    {
        public string Verb = "";                 // GET/POST/... or "?" when not a literal
        public string Url = "";                  // literal parts, holes as {}
        public IMethodSymbol? Callee;
        public SyntaxNode Node = null!;
        public SyntaxNode UrlNode = null!;
        /// <summary>The type serialized as the request body, if the compiler can see it.</summary>
        public ITypeSymbol? Body;
    }

    private static readonly Dictionary<string, string> MapVerbs = new(StringComparer.Ordinal)
    {
        ["MapGet"] = "GET", ["MapPost"] = "POST", ["MapPut"] = "PUT",
        ["MapDelete"] = "DELETE", ["MapPatch"] = "PATCH",
    };

    private static readonly Dictionary<string, string> ClientVerbs = new(StringComparer.Ordinal)
    {
        ["GetAsync"] = "GET", ["GetStringAsync"] = "GET", ["GetByteArrayAsync"] = "GET",
        ["GetStreamAsync"] = "GET", ["PostAsync"] = "POST", ["PutAsync"] = "PUT",
        ["DeleteAsync"] = "DELETE", ["PatchAsync"] = "PATCH",
    };

    private static readonly Dictionary<string, string> AttributeVerbs = new(StringComparer.Ordinal)
    {
        ["HttpGetAttribute"] = "GET", ["HttpPostAttribute"] = "POST", ["HttpPutAttribute"] = "PUT",
        ["HttpDeleteAttribute"] = "DELETE", ["HttpPatchAttribute"] = "PATCH",
    };

    // ------------------------------------------------------------------ endpoints

    /// <summary>endpoints.MapPost("/x", Handler) and friends; MapGroup itself is not an endpoint.</summary>
    public static Endpoint? MinimalEndpoint(InvocationExpressionSyntax inv, SemanticModel model)
    {
        if (inv.Expression is not MemberAccessExpressionSyntax ma) return null;
        var name = ma.Name.Identifier.ValueText;
        if (!MapVerbs.TryGetValue(name, out var verb) && name != "MapMethods") return null;
        if (Method(inv, model) is not { } m || !IsAspNetCoreRouting(m)) return null;
        var args = inv.ArgumentList.Arguments;
        if (args.Count < 2) return null;

        var route = Constant(args[0].Expression, model);
        if (route is null) return null;
        if (name == "MapMethods") verb = MethodsOf(args[1].Expression, model) ?? "*";
        var prefix = GroupPrefix(ma.Expression, model, 0, out var complete);

        // MapPost(route, handler); MapMethods(route, methods, handler).
        var handlerIndex = name == "MapMethods" ? 2 : 1;
        if (args.Count <= handlerIndex) return null;
        var handlerExpr = args[handlerIndex].Expression;
        var e = new Endpoint
        {
            Verb = verb ?? "*", Route = Join(prefix, route), PrefixComplete = complete, Node = inv,
        };
        if (handlerExpr is LambdaExpressionSyntax or AnonymousMethodExpressionSyntax)
            e.HandlerKind = "lambda";
        else
        {
            var info = model.GetSymbolInfo(handlerExpr);
            e.Handler = info.Symbol ?? info.CandidateSymbols.FirstOrDefault();
        }
        var lambdaSym = handlerExpr is LambdaExpressionSyntax l ? model.GetSymbolInfo(l).Symbol as IMethodSymbol : null;
        e.Body = HandlerBody((e.Handler as IMethodSymbol) ?? lambdaSym, e.Verb);
        return e;
    }

    /// <summary>The route prefix a receiver carries: MapGroup("/a").MapGroup("/b") -> "/a/b".</summary>
    private static string GroupPrefix(ExpressionSyntax expr, SemanticModel model, int depth, out bool complete)
    {
        complete = true;
        if (depth > 12) { complete = false; return ""; }
        switch (expr)
        {
            case InvocationExpressionSyntax inv when inv.Expression is MemberAccessExpressionSyntax ma:
            {
                var inner = GroupPrefix(ma.Expression, model, depth + 1, out complete);
                if (ma.Name.Identifier.ValueText == "MapGroup" && inv.ArgumentList.Arguments.Count > 0)
                {
                    var seg = Constant(inv.ArgumentList.Arguments[0].Expression, model);
                    if (seg is null) { complete = false; return inner; }
                    return Join(inner, seg);
                }
                return inner;  // WithTags(), RequireAuthorization(), ... return the same builder
            }
            case IdentifierNameSyntax id:
            {
                var sym = model.GetSymbolInfo(id).Symbol;
                if (sym is ILocalSymbol local
                    && local.DeclaringSyntaxReferences.FirstOrDefault()?.GetSyntax() is VariableDeclaratorSyntax
                       { Initializer.Value: var init })
                    return GroupPrefix(init, model, depth + 1, out complete);
                // A parameter or field: the prefix was built somewhere else.
                if (sym is IParameterSymbol or IFieldSymbol or IPropertySymbol)
                {
                    var t = (sym as IParameterSymbol)?.Type ?? (sym as IFieldSymbol)?.Type ?? (sym as IPropertySymbol)?.Type;
                    complete = t?.Name != "RouteGroupBuilder";
                }
                return "";
            }
            case ParenthesizedExpressionSyntax p:
                return GroupPrefix(p.Expression, model, depth + 1, out complete);
            default:
                return "";
        }
    }

    /// <summary>[HttpPost("x")] on an action of an attribute-routed controller.</summary>
    public static IEnumerable<Endpoint> ControllerEndpoints(IMethodSymbol m, SyntaxNode decl)
    {
        var type = m.ContainingType;
        if (type is null || m.DeclaredAccessibility != Accessibility.Public || m.IsStatic) yield break;
        if (!IsController(type)) yield break;

        var classRoutes = type.GetAttributes().Where(a => a.AttributeClass?.Name == "RouteAttribute")
                              .Select(a => ArgString(a) ?? "").DefaultIfEmpty("").ToList();
        foreach (var a in m.GetAttributes())
        {
            var cls = a.AttributeClass?.Name ?? "";
            string? verb = AttributeVerbs.TryGetValue(cls, out var v) ? v : cls == "RouteAttribute" ? "*" : null;
            if (verb is null) continue;
            var tmpl = ArgString(a) ?? "";
            foreach (var cr in classRoutes)
            {
                var route = tmpl.StartsWith("/") || tmpl.StartsWith("~/") ? tmpl.TrimStart('~') : Join(cr, tmpl);
                route = route.Replace("[controller]", ControllerName(type))
                             .Replace("[action]", m.Name);
                yield return new Endpoint
                {
                    Verb = verb, Route = route, Handler = m, Style = "controller", Node = decl,
                    Body = HandlerBody(m, verb),
                };
            }
        }
    }

    private static bool IsController(INamedTypeSymbol t)
    {
        for (var b = t; b is not null; b = b.BaseType)
        {
            var full = b.ToDisplayString();
            if (full is "Microsoft.AspNetCore.Mvc.ControllerBase" or "System.Web.Http.ApiController"
                or "Microsoft.AspNetCore.Mvc.Controller")
                return true;
        }
        return false;
    }

    private static string ControllerName(INamedTypeSymbol t) =>
        t.Name.EndsWith("Controller", StringComparison.Ordinal) ? t.Name.Substring(0, t.Name.Length - 10) : t.Name;

    private static string? ArgString(AttributeData a) =>
        a.ConstructorArguments.FirstOrDefault(c => c.Value is string).Value as string;

    // ------------------------------------------------------------------ calls

    /// <summary>
    /// A call that sends an HTTP request with a URL the compiler can partly read:
    /// anything passing an HttpMethod plus a string/Uri (custom wrappers,
    /// new HttpRequestMessage(...)), and HttpClient.GetAsync/PostAsync/... .
    /// </summary>
    public static Call? HttpCall(ExpressionSyntax node, SemanticModel model)
    {
        var args = node switch
        {
            InvocationExpressionSyntax i => i.ArgumentList.Arguments,
            ObjectCreationExpressionSyntax o when o.ArgumentList is not null => o.ArgumentList.Arguments,
            _ => default,
        };
        if (args.Count == 0) return null;
        var callee = model.GetSymbolInfo(node).Symbol as IMethodSymbol;
        if (callee is null) return null;

        string? verb = null;
        ExpressionSyntax? url = null;
        if (callee.ContainingType?.ToDisplayString() == "System.Net.Http.HttpClient"
            && ClientVerbs.TryGetValue(callee.Name, out var cv))
        {
            verb = cv;
            url = args[0].Expression;
        }
        else
        {
            foreach (var a in args)
            {
                var t = model.GetTypeInfo(a.Expression).Type;
                if (t?.ToDisplayString() == "System.Net.Http.HttpMethod") verb = VerbOf(a.Expression, model);
                else if (url is null && t is not null
                         && (t.SpecialType == SpecialType.System_String || t.ToDisplayString() == "System.Uri"))
                    url = a.Expression;
            }
            if (verb is null) return null;
        }
        if (url is null) return null;
        var template = Template(url, model, 0);
        // Only a URL with a readable path is linkable; "{}" alone says nothing.
        if (!template.Contains('/') || template.Replace("{}", "").Trim('/').Length == 0) return null;
        return new Call
        {
            Verb = verb, Url = template, Callee = callee, Node = node, UrlNode = url,
            Body = RequestBody(args, url, callee, model),
        };
    }

    private static string VerbOf(ExpressionSyntax e, SemanticModel model)
    {
        switch (e)
        {
            case MemberAccessExpressionSyntax ma when model.GetSymbolInfo(ma).Symbol is IPropertySymbol p
                                                     && p.ContainingType?.ToDisplayString() == "System.Net.Http.HttpMethod":
                return p.Name.ToUpperInvariant();
            case ObjectCreationExpressionSyntax oc when oc.ArgumentList?.Arguments.Count > 0:
                return (Constant(oc.ArgumentList.Arguments[0].Expression, model) ?? "?").ToUpperInvariant();
            default:
                return "?";
        }
    }

    /// <summary>The URL as written: literal text kept, anything computed as "{}".</summary>
    private static string Template(ExpressionSyntax e, SemanticModel model, int depth)
    {
        if (depth > 8) return "{}";
        var c = Constant(e, model);
        if (c is not null) return c;
        switch (e)
        {
            case InterpolatedStringExpressionSyntax s:
            {
                var sb = new StringBuilder();
                foreach (var part in s.Contents)
                {
                    if (part is InterpolatedStringTextSyntax t) sb.Append(t.TextToken.ValueText);
                    else if (part is InterpolationSyntax i)
                        sb.Append(Constant(i.Expression, model) ?? "{}");
                }
                return sb.ToString();
            }
            case BinaryExpressionSyntax b when b.IsKind(SyntaxKind.AddExpression):
                return Template(b.Left, model, depth + 1) + Template(b.Right, model, depth + 1);
            case ParenthesizedExpressionSyntax p:
                return Template(p.Expression, model, depth + 1);
            case ObjectCreationExpressionSyntax oc when oc.ArgumentList?.Arguments.Count > 0
                                                       && model.GetTypeInfo(oc).Type?.ToDisplayString() == "System.Uri":
                return Template(oc.ArgumentList.Arguments[0].Expression, model, depth + 1);
            case IdentifierNameSyntax id
                when model.GetSymbolInfo(id).Symbol is ILocalSymbol local
                     && local.DeclaringSyntaxReferences.FirstOrDefault()?.GetSyntax() is VariableDeclaratorSyntax
                        { Initializer.Value: var init }:
                return Template(init, model, depth + 1);
            case InvocationExpressionSyntax inv
                when model.GetSymbolInfo(inv).Symbol is IMethodSymbol { Name: "Format", ContainingType.SpecialType: SpecialType.System_String }
                     && inv.ArgumentList.Arguments.Count > 0:
            {
                // string.Format("a/{0}/b", x) -> "a/{}/b"
                var fmt = Constant(inv.ArgumentList.Arguments[0].Expression, model);
                return fmt is null ? "{}" : System.Text.RegularExpressions.Regex.Replace(fmt, @"\{\d+(?:[,:][^}]*)?\}", "{}");
            }
            default:
                return "{}";
        }
    }

    // ------------------------------------------------------------------ bodies

    /// <summary>
    /// What a call sends as its body: the argument of a JsonConvert.SerializeObject /
    /// JsonSerializer.Serialize found inside one of its arguments (how the
    /// NextApiClient wrapper and StringContent callers do it), or the value of a
    /// PostAsJsonAsync-style call.
    /// </summary>
    private static ITypeSymbol? RequestBody(SeparatedSyntaxList<ArgumentSyntax> args, ExpressionSyntax url,
                                            IMethodSymbol callee, SemanticModel model)
    {
        foreach (var a in args)
        {
            if (a.Expression == url) continue;
            foreach (var inv in a.Expression.DescendantNodesAndSelf().OfType<InvocationExpressionSyntax>())
            {
                if (model.GetSymbolInfo(inv).Symbol is IMethodSymbol m
                    && m.Name is "SerializeObject" or "Serialize"
                    && inv.ArgumentList.Arguments.Count > 0)
                {
                    var t = model.GetTypeInfo(inv.ArgumentList.Arguments[0].Expression).Type;
                    if (Interesting(t) is { } bt) return bt;
                }
            }
        }
        if (callee.Name.EndsWith("AsJsonAsync", StringComparison.Ordinal) && args.Count > 1)
            return Interesting(model.GetTypeInfo(args[1].Expression).Type);
        return null;
    }

    /// <summary>[FromBody] parameter, else the first concrete source-declared class/record/struct parameter.</summary>
    private static ITypeSymbol? HandlerBody(IMethodSymbol? m, string verb)
    {
        if (m is null) return null;
        foreach (var p in m.Parameters)
            if (p.GetAttributes().Any(a => a.AttributeClass?.Name is "FromBodyAttribute"))
                return Interesting(p.Type);
        if (verb is "GET" or "DELETE") return null;
        foreach (var p in m.Parameters)
        {
            if (p.GetAttributes().Any(a => a.AttributeClass?.Name is "FromServicesAttribute" or "FromRouteAttribute"
                                                                    or "FromQueryAttribute" or "FromHeaderAttribute"))
                continue;
            if (Interesting(p.Type) is { } t && t.TypeKind is TypeKind.Class or TypeKind.Struct && !t.IsAbstract)
                return t;
        }
        return null;
    }

    /// <summary>A source-declared type, looking through arrays and one level of generic collection.</summary>
    private static ITypeSymbol? Interesting(ITypeSymbol? t)
    {
        if (t is null) return null;
        if (t is IArrayTypeSymbol arr) t = arr.ElementType;
        if (t is INamedTypeSymbol { IsGenericType: true } g && !g.Locations.Any(l => l.IsInSource))
        {
            var inner = g.TypeArguments.FirstOrDefault(a => a.Locations.Any(l => l.IsInSource));
            if (inner is not null) t = inner;
        }
        return t.Locations.Any(l => l.IsInSource) && t.TypeKind is TypeKind.Class or TypeKind.Struct or TypeKind.Enum
            ? t
            : null;
    }

    // ------------------------------------------------------------------ helpers

    private static IMethodSymbol? Method(InvocationExpressionSyntax inv, SemanticModel model)
    {
        var info = model.GetSymbolInfo(inv);
        return (info.Symbol ?? info.CandidateSymbols.FirstOrDefault()) as IMethodSymbol;
    }

    private static bool IsAspNetCoreRouting(IMethodSymbol m)
    {
        var ns = (m.ReducedFrom ?? m).ContainingNamespace?.ToDisplayString() ?? "";
        return ns.StartsWith("Microsoft.AspNetCore", StringComparison.Ordinal);
    }

    private static string? MethodsOf(ExpressionSyntax e, SemanticModel model)
    {
        var parts = e.DescendantNodesAndSelf().OfType<LiteralExpressionSyntax>()
                     .Select(l => model.GetConstantValue(l).Value as string)
                     .Where(s => !string.IsNullOrEmpty(s)).Select(s => s!.ToUpperInvariant()).ToList();
        return parts.Count == 0 ? null : string.Join(",", parts);
    }

    private static string? Constant(ExpressionSyntax e, SemanticModel model)
    {
        var cv = model.GetConstantValue(e);
        return cv.HasValue ? cv.Value as string : null;
    }

    private static string Join(string a, string b)
    {
        if (string.IsNullOrEmpty(a)) return b;
        if (string.IsNullOrEmpty(b)) return a;
        return a.TrimEnd('/') + "/" + b.TrimStart('/');
    }
}
