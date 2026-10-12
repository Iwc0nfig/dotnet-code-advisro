<%@ Page Language="C#" CodeFile="Default.aspx.cs" Inherits="DemoPage" MasterPageFile="~/Site.master" %>
<asp:Content ID="Body" ContentPlaceHolderID="Main" runat="server">
  <asp:TextBox ID="SearchText" runat="server" />
  <asp:Button ID="SearchButton" Text="Search" runat="server" OnClick="SearchButton_Click" OnClientClick="return demo.orders.validate();" />
  <asp:Label ID="ResultLabel" runat="server" />
  <%-- <asp:Label ID="HiddenComment" runat="server" /> --%>
</asp:Content>
