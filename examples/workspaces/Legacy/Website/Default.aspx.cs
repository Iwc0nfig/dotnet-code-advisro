using System;
using System.Web.UI;
using Demo.Shared;

public partial class DemoPage : Page
{
    protected void Page_Load(object sender, EventArgs e)
    {
        if (!IsPostBack) ResultLabel.Text = OrderService.Find(1);
    }

    protected void SearchButton_Click(object sender, EventArgs e)
    {
        ResultLabel.Text = SearchText.Text;
    }
}
